#!/usr/bin/env python3
"""Bot HQ (trader-bot): a team of agents that trades five markets.

Standard library only. Long or flat, never short, never leveraged.

Two strategy desks share the team; the Skeptic only lets a desk trade once
its backtest made money after fees:

  Trend Rider (daily bars, LIVE): holds a market while its close is above the
    EMA 200 and the EMA 50 is above the EMA 200 (score 70+), sized so each
    market carries about the same risk (10% a year spread across five, at
    most 40% of the account in one), costs on every change. Decided at the
    close, earns from the next day. Tested since 2017.
  Scalper (hourly bars, BENCHED): the stack from the videos, below. Kept so
    its record stays visible: it lost money after fees in the backtest.

The Scalper's team, in the order a trade passes through it:
  Scout    reads each hourly bar: EMA 50 and 200, RSI 14, Aroon 25, relative
           volume, ATR 14, and 20-bar momentum against the S&P 500.
  Scorer   turns that into a score from 0 to 100 (five parts of 20 each).
  Skeptic  vetoes: chasing (RSI over 75), a falling knife (a 24-bar drop of
           more than 3 ATR), and, live, a crash or bear market type.
  Risk     risk 1% of equity per trade, stop 2 ATR under the entry, target
           3 ATR over it, at most 25% of equity per market, never more than
           100% in total.
  Executor enters at the NEXT bar's open, never the bar it decided on.
           Fees on both sides of every trade.
  Reporter the morning brief and the evening summary.

Exits: the stop or target (if one bar touches both, the stop is assumed:
the worst case), a close back under the EMA 200 (rule 4 of the "EMA 50 &
200" post), or 72 bars held. A rule exit is decided at the close and filled
at the next open.

The rules below were fixed before the first backtest and are not tuned on
it, so every year of the backtest is out of sample for them.

    python3 bot.py backtest --out bot.json     # 2 years of hourly bars
    python3 bot.py report --file bot.json --kind evening
"""

import argparse
import datetime
import json
import math
import os
import sys
import time
import urllib.request

UA = "Mozilla/5.0 (day-trader-skill bot)"

MARKETS = [
    # symbol, name, fee per side
    ("BTC-USD", "Bitcoin", 0.0025),
    ("QQQ", "Nasdaq 100", 0.0005),
    ("SPY", "S&P 500", 0.0005),
    ("GLD", "Gold", 0.0005),
    ("USO", "Oil", 0.0005),
]
BENCH = "SPY"

RULES = {
    "threshold": 70,       # score needed to trade
    "risk_pct": 1.0,       # of equity, per trade
    "stop_atr": 2.0,
    "target_atr": 3.0,
    "max_hold": 72,        # bars
    "max_weight": 0.25,    # of equity per market
    "max_exposure": 1.0,   # of equity in total
    "rsi_veto": 75,
    "knife_atr": 3.0,
}
START_EQUITY = 10000.0


class BotError(Exception):
    pass


# ---------------------------------------------------------------- data

def fetch_hourly(sym, rng="730d", timeout=30):
    """[[unix, open, high, low, close, volume], ...] from Yahoo's free chart. Unofficial."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1h"
           % (urllib.request.quote(sym), rng))
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                doc = json.load(r)
            res = doc["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            out = []
            for i, t in enumerate(res.get("timestamp") or []):
                o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
                if None in (o, h, l, c) or c <= 0:
                    continue
                out.append([int(t), float(o), float(h), float(l), float(c), float(v or 0)])
            # the newest bar is often still forming (volume 0): drop it
            if out and out[-1][5] == 0 and len(out) > 1 and out[-2][5] > 0:
                out.pop()
            return out
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise BotError("could not fetch %s from Yahoo: %s" % (sym, last))


# ---------------------------------------------------------------- Scout

def ema(xs, n):
    out, k, e = [None] * len(xs), 2.0 / (n + 1), None
    for i, x in enumerate(xs):
        if i < n - 1:
            continue
        e = sum(xs[i - n + 1:i + 1]) / n if e is None else x * k + e * (1 - k)
        out[i] = e
    return out


def rsi(xs, n=14):
    out = [None] * len(xs)
    if len(xs) <= n:
        return out
    gains = [max(xs[i] - xs[i - 1], 0) for i in range(1, n + 1)]
    losses = [max(xs[i - 1] - xs[i], 0) for i in range(1, n + 1)]
    ag, al = sum(gains) / n, sum(losses) / n
    for i in range(n, len(xs)):
        if i > n:
            d = xs[i] - xs[i - 1]
            ag = (ag * (n - 1) + max(d, 0)) / n
            al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(bars, n=14):
    out, a = [None] * len(bars), None
    for i in range(1, len(bars)):
        h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if i < n:
            continue
        if a is None:
            a = sum(max(bars[j][2] - bars[j][3], abs(bars[j][2] - bars[j - 1][4]), abs(bars[j][3] - bars[j - 1][4]))
                    for j in range(i - n + 1, i + 1)) / n
        else:
            a = (a * (n - 1) + tr) / n
        out[i] = a
    return out


def aroon(bars, n=25):
    """Aroon oscillator: (up - down), from -100 to 100. Up = 100 * (n - bars since the n-bar high) / n."""
    out = [None] * len(bars)
    for i in range(n, len(bars)):
        win = bars[i - n:i + 1]
        hi = max(range(len(win)), key=lambda j: (win[j][2], j))
        lo = max(range(len(win)), key=lambda j: (-win[j][3], j))
        up = 100.0 * (n - (n - hi)) / n
        dn = 100.0 * (n - (n - lo)) / n
        out[i] = up - dn
    return out


def scout(bars, bench=None):
    """Per-bar readings. bench: {unix: close} of the S&P 500 for relative momentum."""
    c = [b[4] for b in bars]
    e50, e200, r, a, osc = ema(c, 50), ema(c, 200), rsi(c, 14), atr(bars, 14), aroon(bars, 25)
    bt = sorted(bench) if bench else []
    out = []
    j = -1
    for i, b in enumerate(bars):
        vols = [x[5] for x in bars[max(0, i - 20):i] if x[5] > 0]
        rvol = (b[5] / (sum(vols) / len(vols))) if vols and b[5] > 0 else None
        mom = (c[i] / c[i - 20] - 1) if i >= 20 else None
        rel = None
        if mom is not None and bt:
            while j + 1 < len(bt) and bt[j + 1] <= b[0]:
                j += 1
            if j >= 0:
                # S&P 500's 20-bar move as of this bar (its own bars)
                k = j - 20
                if k >= 0:
                    rel = mom - (bench[bt[j]] / bench[bt[k]] - 1)
        out.append({"ema50": e50[i], "ema200": e200[i], "rsi": r[i], "atr": a[i], "aroon": osc[i],
                    "rvol": rvol, "mom": mom, "rel": rel if rel is not None else mom})
    return out


# ---------------------------------------------------------------- Scorer and Skeptic

def score(bar, s):
    """0-100 from five parts of 20, plus the parts themselves. None while indicators warm up."""
    if None in (s["ema50"], s["ema200"], s["rsi"], s["atr"], s["aroon"], s["rel"]) or s["atr"] <= 0:
        return None, {}
    c, lo = bar[4], bar[3]
    trend = (10 if c > s["ema200"] else 0) + (10 if s["ema50"] > s["ema200"] else 0)
    # entry spot: pulled back to the EMA 50 and held it (rules 2-3 of the post), or close above it
    d50 = (c - s["ema50"]) / s["atr"]
    if lo <= s["ema50"] + 0.25 * s["atr"] and c > s["ema50"]:
        entry = 20
    elif 0 <= d50 <= 1.5:
        entry = 10
    else:
        entry = 0
    strength = max(0.0, s["aroon"]) / 100 * 20
    rv = s["rvol"]
    volume = 10.0 if rv is None else max(0.0, min(20.0, (rv - 0.8) / 0.6 * 20))
    momentum = max(0.0, min(20.0, 10 + s["rel"] / 0.02 * 10))
    parts = {"trend": trend, "entry": entry, "strength": round(strength, 1),
             "volume": round(volume, 1), "momentum": round(momentum, 1)}
    return round(sum(parts.values()), 1), parts


def skeptic(bars, i, s, rules=RULES, market_type=None):
    """List of vetoes; empty means no objection."""
    v = []
    if s["rsi"] is not None and s["rsi"] > rules["rsi_veto"]:
        v.append("chasing: RSI %.0f" % s["rsi"])
    if i >= 24 and s["atr"]:
        drop = bars[i - 24][4] - bars[i][4]
        if drop > rules["knife_atr"] * s["atr"]:
            v.append("falling knife: down %.1f ATR in 24 bars" % (drop / s["atr"]))
    if market_type in ("crash", "bear"):
        v.append("market type is %s" % market_type)
    return v


# ---------------------------------------------------------------- the simulation (Risk + Executor)

def simulate(data, rules=RULES, start_equity=START_EQUITY, start_ts=None):
    """data: {sym: {"bars": [...], "scout": [...], "fee": f, "name": n}}.

    One shared account across all markets. Returns trades, the equity at every
    hour any market printed, and the open positions at the end."""
    events = []
    for sym, d in data.items():
        for i, b in enumerate(d["bars"]):
            if start_ts is None or b[0] >= start_ts:
                events.append((b[0], sym, i))
    events.sort()
    cash, pos, pending, last = start_equity, {}, {}, {}
    trades, curve = [], []

    def equity():
        return cash + sum(p["qty"] * last.get(s, p["entry"]) for s, p in pos.items())

    def close(sym, px, ts, why):
        nonlocal cash
        p = pos.pop(sym)
        fee = data[sym]["fee"]
        cash += p["qty"] * px * (1 - fee)
        pnl = p["qty"] * (px * (1 - fee) - p["entry"] * (1 + fee))
        trades.append({"symbol": sym, "entry_ts": p["ts"], "exit_ts": ts, "entry": round(p["entry"], 4),
                       "exit": round(px, 4), "qty": p["qty"], "pnl": round(pnl, 2),
                       "pct": round((px * (1 - fee)) / (p["entry"] * (1 + fee)) * 100 - 100, 3),
                       "score": p["score"], "why": why, "bars": p["held"]})

    for ts, sym, i in events:
        d = data[sym]
        b, s = d["bars"][i], d["scout"][i]
        o, h, l, c = b[1], b[2], b[3], b[4]
        fee = d["fee"]
        # 1. orders decided at the last close fill at this open
        act = pending.pop(sym, None)
        if act and act["kind"] == "exit" and sym in pos:
            close(sym, o, ts, act["why"])
        elif act and act["kind"] == "entry" and sym not in pos:
            eq = equity()
            stop_d = rules["stop_atr"] * act["atr"]
            qty = (eq * rules["risk_pct"] / 100) / stop_d
            qty = min(qty, eq * rules["max_weight"] / o)
            room = eq * rules["max_exposure"] - sum(p["qty"] * last.get(x, p["entry"]) for x, p in pos.items())
            qty = min(qty, max(0.0, room) / o, cash / (o * (1 + fee)))
            if qty * o >= 1:
                cash -= qty * o * (1 + fee)
                pos[sym] = {"qty": qty, "entry": o, "ts": ts, "stop": o - stop_d,
                            "target": o + rules["target_atr"] * act["atr"], "score": act["score"], "held": 0}
        # 2. stops and targets inside this bar (stop first: the worst case)
        if sym in pos:
            p = pos[sym]
            p["held"] += 1
            if l <= p["stop"]:
                close(sym, min(o, p["stop"]), ts, "stop")
            elif h >= p["target"]:
                close(sym, max(o, p["target"]), ts, "target")
        last[sym] = c
        # 3. decisions at this close, filled at the next open
        if sym in pos:
            p = pos[sym]
            if s["ema200"] is not None and c < s["ema200"]:
                pending[sym] = {"kind": "exit", "why": "closed under EMA 200"}
            elif p["held"] >= rules["max_hold"]:
                pending[sym] = {"kind": "exit", "why": "time stop"}
        else:
            sc, _ = score(b, s)
            if sc is not None and sc >= rules["threshold"] and not skeptic(d["bars"], i, s, rules):
                pending[sym] = {"kind": "entry", "atr": s["atr"], "score": sc}
        curve.append((ts, equity()))
    return {"trades": trades, "curve": curve, "open": pos, "cash": cash}


# ---------------------------------------------------------------- reading the result

def day(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def daily_equity(curve):
    out = {}
    for ts, eq in curve:
        out[day(ts)] = eq
    return sorted(out.items())


def max_drawdown(values):
    peak, dd = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak if peak > 0 else 0)
    return dd


def stats(trades, deq, start_equity):
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    vals = [v for _, v in deq]
    end = vals[-1] if vals else start_equity
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals))]
    sd = (sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 if len(rets) > 2 else 0
    return {
        "start": round(start_equity, 2), "end": round(end, 2),
        "return_pct": round((end / start_equity - 1) * 100, 2),
        "max_drawdown_pct": round(max_drawdown([start_equity] + vals) * 100, 2),
        "trades": len(trades), "wins": len(wins),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "sharpe": round(sum(rets) / len(rets) / sd * (365 ** 0.5), 2) if sd > 0 else 0,
        "green_days": sum(1 for r in rets if r > 0), "red_days": sum(1 for r in rets if r < 0),
    }


def hold_curve(data, days):
    """Equal money in each market on the first day, then left alone (buy and hold)."""
    closes = {}
    for sym, d in data.items():
        m = {}
        for b in d["bars"]:
            m[day(b[0])] = b[4]
        closes[sym] = m
    first, out = {}, []
    for dd in days:
        tot, n = 0.0, 0
        for sym, m in closes.items():
            # the last close on or before this day
            px = m.get(dd)
            if px is None:
                prior = [k for k in m if k <= dd]
                px = m[max(prior)] if prior else None
            if px is None:
                continue
            first.setdefault(sym, px)
            tot += px / first[sym]
            n += 1
        out.append(tot / n if n else 1.0)
    return out


def backtest(data, rules=RULES, start_equity=START_EQUITY):
    sim = simulate(data, rules, start_equity)
    deq = daily_equity(sim["curve"])
    days = [d for d, _ in deq]
    hold = hold_curve(data, days)
    trades = sim["trades"]
    by_day = {}
    for t in trades:
        k = day(t["exit_ts"])
        by_day.setdefault(k, [0.0, 0])
        by_day[k][0] += t["pnl"]
        by_day[k][1] += 1
    cal, prev = [], start_equity
    for d, eq in deq:
        cal.append({"date": d, "pnl": round(eq - prev, 2), "closed": by_day.get(d, [0, 0])[1]})
        prev = eq
    per = {}
    for sym in data:
        ts = [t for t in trades if t["symbol"] == sym]
        pnl = sum(t["pnl"] for t in ts)
        w = [t for t in ts if t["pnl"] > 0]
        loss = -sum(t["pnl"] for t in ts if t["pnl"] <= 0)
        b = data[sym]["bars"]
        per[sym] = {"name": data[sym]["name"], "trades": len(ts), "pnl": round(pnl, 2),
                    "win_rate_pct": round(len(w) / len(ts) * 100, 1) if ts else 0,
                    "profit_factor": round(sum(t["pnl"] for t in w) / loss, 2) if loss > 0 else None,
                    "hold_pct": round((b[-1][4] / b[0][4] - 1) * 100, 1) if b else None}
    # each year on its own: does it hold up, or was it one lucky stretch?
    years = {}
    for d, eq in deq:
        years.setdefault(d[:4], []).append(eq)
    by_year, prev = {}, start_equity
    for y in sorted(years):
        by_year[y] = round((years[y][-1] / prev - 1) * 100, 2)
        prev = years[y][-1]
    st = stats(trades, deq, start_equity)
    st["hold_return_pct"] = round((hold[-1] - 1) * 100, 2) if hold else None
    st["hold_max_drawdown_pct"] = round(max_drawdown(hold) * 100, 2) if hold else None
    st["by_year"] = by_year
    st["from"], st["to"] = (days[0], days[-1]) if days else (None, None)
    return {"stats": st, "markets": per, "calendar": cal,
            "equity": [[d, round(v, 2), round(h * start_equity, 2)] for (d, v), h in zip(deq, hold)],
            "trades": trades}


def now(data, rules=RULES, market_type=None):
    """What every agent sees on the latest bar of each market."""
    rows = []
    for sym, d in data.items():
        i = len(d["bars"]) - 1
        b, s = d["bars"][i], d["scout"][i]
        sc, parts = score(b, s)
        veto = skeptic(d["bars"], i, s, rules, market_type)
        if sc is None:
            verdict = "warming up"
        elif veto:
            verdict = "vetoed"
        elif sc >= rules["threshold"]:
            verdict = "BUY at next open"
        else:
            verdict = "wait"
        rows.append({"symbol": sym, "name": d["name"], "ts": b[0], "price": b[4], "score": sc, "parts": parts,
                     "veto": veto, "verdict": verdict,
                     "rsi": round(s["rsi"], 1) if s["rsi"] is not None else None,
                     "aroon": round(s["aroon"], 0) if s["aroon"] is not None else None,
                     "above_ema200": bool(s["ema200"] and b[4] > s["ema200"]),
                     "stop": round(b[4] - rules["stop_atr"] * s["atr"], 4) if s["atr"] else None,
                     "target": round(b[4] + rules["target_atr"] * s["atr"], 4) if s["atr"] else None})
    return rows


def load(fetch=None, markets=MARKETS):
    fetch = fetch or fetch_hourly
    raw = {}
    for sym, name, fee in markets:
        raw[sym] = (fetch(sym), name, fee)
    bench = {b[0]: b[4] for b in raw[BENCH][0]} if BENCH in raw else None
    return {sym: {"bars": bars, "scout": scout(bars, None if sym == BENCH else bench), "fee": fee, "name": name}
            for sym, (bars, name, fee) in raw.items()}


# ---------------------------------------------------------------- Reporter

def money(v):
    return ("-$" if v < 0 else "$") + "{:,.2f}".format(abs(v))


def report(doc, kind="evening"):
    tr, rows = doc["trend"]["stats"], doc["trend_now"]
    lines = []
    if kind == "morning":
        lines.append("Bot HQ morning brief (%s)" % doc["written"][:10])
        for r in rows:
            lines.append("- %s %s: trend score %s/100, %s%s" % (
                r["name"], money(r["price"]), "?" if r["score"] is None else int(r["score"]),
                ("HOLD %.0f%% of the account" % (r["weight"] * 100)) if r["weight"] else r["verdict"],
                (" (" + "; ".join(r["veto"]) + ")") if r["veto"] else ""))
        hot = [r for r in doc.get("scalper_now", []) if r["score"] is not None and r["score"] >= 70]
        if hot:
            lines.append("Scalper (benched, not trading) would have liked: " + ", ".join(r["name"] for r in hot))
    else:
        p = doc.get("paper")
        lines.append("Bot HQ evening report (%s)" % doc["written"][:10])
        if p:
            lines.append("Paper account since %s: %s (%+.2f%%), %d trades closed, %d open"
                         % (p["since"], money(p["equity"]), p["return_pct"], p["trades"], len(p["open"])))
            for o in p["open"]:
                lines.append("- holding %s since %s, %s" % (o["symbol"], o["since"], money(o["pnl"])))
        lines.append("Trend Rider backtest %s to %s: %+.0f%% (holding all five: %+.0f%%); worst drop %.1f%% (holding: %.1f%%)"
                     % (tr["from"], tr["to"], tr["return_pct"], tr["hold_return_pct"], tr["max_drawdown_pct"],
                        tr["hold_max_drawdown_pct"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- Trend Rider (daily)

TREND = {"target_vol": 0.10, "max_weight": 0.40, "start": "2017-09-01"}
DAYS_PER_YEAR = {"BTC-USD": 365}


def fetch_daily(sym, rng="10y"):
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "trader-risk"))
    import correlation  # noqa: E402
    return correlation.fetch_daily(sym, rng)


def trend_score(c, e50, e200, osc):
    """0-99: 40 for a close above the EMA 200, 30 for the EMA 50 above it, up to 29 for Aroon strength.
    70 or more means both trend tests pass: the Trend Rider holds it."""
    if e200 is None or e50 is None:
        return None, {}
    parts = {"above_200": 40 if c > e200 else 0, "50_over_200": 30 if e50 > e200 else 0,
             "strength": round(max(0.0, osc or 0) / 100 * 29, 1)}
    return round(sum(parts.values()), 1), parts


def trend_series(closes):
    """closes: [[date, close], ...] -> per day: close, score, parts, annualised 20-day volatility."""
    c = [x[1] for x in closes]
    e50, e200 = ema(c, 50), ema(c, 200)
    pseudo = [[0, x, x, x, x, 0] for x in c]        # Aroon on closes
    osc = aroon(pseudo, 25)
    out = {}
    for i, (d, px) in enumerate(closes):
        sc, parts = trend_score(px, e50[i], e200[i], osc[i])
        win = c[max(0, i - 20):i + 1]
        rets = [win[k] / win[k - 1] - 1 for k in range(1, len(win))]
        vol = (sum(r * r for r in rets) / len(rets)) ** 0.5 if rets else None
        out[d] = {"close": px, "prev": c[i - 1] if i else None, "score": sc, "parts": parts, "vol": vol,
                  "ema50": e50[i], "ema200": e200[i], "aroon": osc[i]}
    return out


def trend_weights(today, syms, cfg=TREND):
    """Target weights from each market's row for today (missing rows keep no position)."""
    w = {}
    n = len(syms)
    for s in syms:
        r = today.get(s)
        if not r or r["score"] is None or r["score"] < 70 or not r["vol"]:
            w[s] = 0.0
            continue
        ann = r["vol"] * (DAYS_PER_YEAR.get(s, 252) ** 0.5)
        w[s] = min(cfg["max_weight"], cfg["target_vol"] / (n ** 0.5) / ann)
    tot = sum(w.values())
    if tot > 1:
        w = {k: v / tot for k, v in w.items()}
    return w


def trend_run(series, fees, cfg=TREND, start=None, start_equity=START_EQUITY):
    """series: {sym: trend_series(...)}. Day by day on the S&P 500's calendar plus crypto weekends."""
    syms = list(series)
    start = start or cfg["start"]
    days = sorted(set(d for s in syms for d in series[s] if d >= start))
    eq, heq = start_equity, start_equity
    w = {s: 0.0 for s in syms}
    curve, trades, opened, pnl_open = [], [], {}, {}
    for d in days:
        today = {s: series[s].get(d) for s in syms}
        # yesterday's weights earn today's moves; markets that are shut today don't move
        day_pnl, hrets = 0.0, []
        for s in syms:
            r = today[s]
            if r and r["prev"]:
                ret = r["close"] / r["prev"] - 1
                gain = w[s] * eq * ret
                day_pnl += gain
                if s in opened:
                    pnl_open[s] += gain
                hrets.append(ret)
        eq += day_pnl
        heq *= 1 + sum(hrets) / len(syms)   # equal money in all five; a shut market moves 0
        nw = trend_weights({s: today[s] for s in syms if today[s]}, syms, cfg)
        for s in syms:
            if not today[s]:
                nw[s] = w[s]              # market shut: keep what we hold
        for s in syms:
            fee_s = abs(nw[s] - w[s]) * eq * fees[s]
            eq -= fee_s
            if w[s] == 0 and nw[s] > 0:
                opened[s] = {"date": d, "price": today[s]["close"], "score": today[s]["score"]}
                pnl_open[s] = -fee_s
            elif w[s] > 0 and nw[s] == 0 and s in opened:
                o = opened.pop(s)
                p = pnl_open.pop(s) - fee_s
                trades.append({"symbol": s, "entry_date": o["date"], "exit_date": d, "entry": round(o["price"], 4),
                               "exit": round(today[s]["close"], 4), "pnl": round(p, 2),
                               "pct": round((today[s]["close"] / o["price"] - 1) * 100, 2),
                               "score": o["score"], "why": "trend broke (score under 70)"})
            elif s in opened:
                pnl_open[s] -= fee_s
        w = nw
        curve.append([d, round(eq, 2), round(heq, 2), {s: round(v, 4) for s, v in w.items() if v}])
    open_pos = [{"symbol": s, "since": o["date"], "entry": round(o["price"], 4), "score": o["score"],
                 "pnl": round(pnl_open[s], 2), "weight": round(w[s], 4)} for s, o in opened.items()]
    return {"curve": curve, "trades": trades, "open": open_pos, "weights": w}


def trend_report(run, start_equity=START_EQUITY, names=None):
    curve, trades = run["curve"], run["trades"]
    vals = [c[1] for c in curve]
    hold = [c[2] for c in curve]
    deq = [(c[0], c[1]) for c in curve]
    st = stats(trades, deq, start_equity)
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals))]
    m = sum(rets) / len(rets) if rets else 0
    sd = (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5 if rets else 0
    st["sharpe"] = round(m / sd * 252 ** 0.5, 2) if sd else 0
    hr = [hold[i] / hold[i - 1] - 1 for i in range(1, len(hold))]
    hm = sum(hr) / len(hr) if hr else 0
    hs = (sum((r - hm) ** 2 for r in hr) / len(hr)) ** 0.5 if hr else 0
    st["hold_return_pct"] = round((hold[-1] / start_equity - 1) * 100, 2) if hold else None
    st["hold_max_drawdown_pct"] = round(max_drawdown([start_equity] + hold) * 100, 2) if hold else None
    st["hold_sharpe"] = round(hm / hs * 252 ** 0.5, 2) if hs else 0
    st["avg_exposure_pct"] = round(sum(sum(c[3].values()) for c in curve) / len(curve) * 100, 1) if curve else 0
    years, prev, hprev, by_year = {}, start_equity, start_equity, {}
    for c in curve:
        years.setdefault(c[0][:4], []).append(c)
    for y in sorted(years):
        e, h = years[y][-1][1], years[y][-1][2]
        by_year[y] = {"bot": round((e / prev - 1) * 100, 2), "hold": round((h / hprev - 1) * 100, 2)}
        prev, hprev = e, h
    st["by_year"] = by_year
    st["from"], st["to"] = (curve[0][0], curve[-1][0]) if curve else (None, None)
    cal, prev = [], start_equity
    closed = {}
    for t in trades:
        closed[t["exit_date"]] = closed.get(t["exit_date"], 0) + 1
    for c in curve:
        cal.append({"date": c[0], "pnl": round(c[1] - prev, 2), "closed": closed.get(c[0], 0)})
        prev = c[1]
    per = {}
    for s in (names or {}):
        ts = [t for t in trades if t["symbol"] == s]
        w = [t for t in ts if t["pnl"] > 0]
        loss = -sum(t["pnl"] for t in ts if t["pnl"] <= 0)
        per[s] = {"name": names[s], "trades": len(ts), "pnl": round(sum(t["pnl"] for t in ts), 2),
                  "win_rate_pct": round(len(w) / len(ts) * 100, 1) if ts else 0,
                  "profit_factor": round(sum(t["pnl"] for t in w) / loss, 2) if loss > 0 else None}
    return {"stats": st, "markets": per, "calendar": cal,
            "equity": [[c[0], c[1], c[2]] for c in curve], "trades": trades, "open": run["open"]}


def trend_now(series, names, cfg=TREND, market_type=None):
    syms = list(series)
    last = {s: series[s][max(series[s])] for s in syms}
    w = trend_weights(last, syms, cfg)
    rows = []
    for s in syms:
        d = max(series[s])
        r = last[s]
        veto = ["market type is %s" % market_type] if market_type in ("crash",) and w[s] else []
        rows.append({"symbol": s, "name": names[s], "date": d, "price": r["close"], "score": r["score"],
                     "parts": r["parts"], "weight": round(w[s], 4), "veto": veto,
                     "verdict": "HOLD" if w[s] and not veto else ("vetoed" if veto else "stay out"),
                     "ema200": round(r["ema200"], 4) if r["ema200"] else None,
                     "vol_pct": round(r["vol"] * (DAYS_PER_YEAR.get(s, 252) ** 0.5) * 100, 1) if r["vol"] else None})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bot HQ: the agent team, its backtests and its paper account.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("run", help="fetch, backtest both desks, read today, write one file")
    b.add_argument("--out", default="bot.json")
    b.add_argument("--live-since", default="2026-11-05", help="YYYY-MM-DD the paper account started")
    b.add_argument("--market-type", help="crash/bear/neutral/bull/euphoria from trader-regime")
    r = sub.add_parser("report")
    r.add_argument("--file", default="bot.json")
    r.add_argument("--kind", choices=["morning", "evening"], default="evening")
    a = ap.parse_args(argv)
    if a.cmd == "report":
        with open(a.file, encoding="utf-8") as fh:
            print(report(json.load(fh), a.kind))
        return 0
    names = {s: n for s, n, f in MARKETS}
    fees = {s: f for s, n, f in MARKETS}
    daily = {s: fetch_daily(s) for s in names}
    series = {s: trend_series(daily[s]) for s in names}
    hourly = load()
    scal = backtest(hourly)
    trend = trend_report(trend_run(series, fees), names=names)
    live = trend_run(series, fees, start=a.live_since)
    lr = trend_report(live, names=names) if live["curve"] else None
    doc = {"written": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "market_type": a.market_type,
           "markets": [{"symbol": s, "name": n, "fee_pct": f * 100} for s, n, f in MARKETS],
           "trend_rules": TREND, "scalper_rules": RULES,
           "trend": trend, "trend_now": trend_now(series, names, market_type=a.market_type),
           "scalper": {"stats": scal["stats"], "markets": scal["markets"],
                       "equity": scal["equity"][::1], "trades": scal["trades"][-40:]},
           "scalper_now": now(hourly, market_type=a.market_type),
           "paper": {"since": a.live_since,
                     "equity": lr["stats"]["end"] if lr else START_EQUITY,
                     "return_pct": lr["stats"]["return_pct"] if lr else 0.0,
                     "trades": len(live["trades"]), "closed": live["trades"], "open": live["open"],
                     "equity_curve": lr["equity"] if lr else []}}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    print(report(doc, "morning"))
    print(report(doc, "evening"))
    st = scal["stats"]
    print("Scalper (benched): %+.1f%% vs holding %+.1f%%, %d trades" % (st["return_pct"], st["hold_return_pct"], st["trades"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
