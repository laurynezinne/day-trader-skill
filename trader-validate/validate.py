#!/usr/bin/env python3
"""The gauntlet (trader-validate). Standard library only.

A strategy earns paper money only after it survives this. Daily bars, long
only, costs on every trade, and every choice made on past data alone:

  walk-forward   for each test year, the strategy's settings are picked on the
                 TRAIN years before it, then run untouched on that year. The
                 stitched test years are the only results that count.
  filters        out-of-sample Sharpe > 0.5, max drawdown < 35%, at least 30
                 trades, out-of-sample Sharpe at least half the in-sample one
                 (the overfitting check), and a 500-resample bootstrap whose
                 5th-percentile Sharpe stays above 0.
  baselines      buy-and-hold, the 200-day-average rule and random entries
                 with the same time in the market, on the same test years.

Strategies:
  rsi_reversion  buy when RSI(2) closes under a threshold while price is above
                 its 200-day average; sell when the close is above the N-day
                 average. (Mean reversion: the one family that held up on
                 average across 9,000 backtests.)
  sma_trend      hold while the close is above its N-day average.
  ema_pullback   the "EMA 50 & 200" post, long side: with the 50 EMA above the
                 200, buy a close that pulls back to within 0.5-2% of the 50;
                 quit on a close below the 200.
  ema_trend      hold while the close is above its 100- or 200-day EMA.

    python3 validate.py run SPY QQQ AAPL MSFT --strategy rsi_reversion
    python3 validate.py run --universe default --json
    python3 validate.py run SPY --closes trader/state/history.json     # offline

Writes trader/state/validation.json; the auto runner only trades what PASSED.
"""

import argparse
import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "trader-risk"))

OUT_PATH = "trader/state/validation.json"
COST = 0.0005            # per side: commission + slippage, 0.05%
TRAIN_YEARS = 3
MIN_TRADES = 30
MIN_SHARPE = 0.5
MAX_DD = 35.0
DECAY = 0.5              # OOS Sharpe must be >= DECAY x IS Sharpe
BOOT = 500
DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META",
                    "JPM", "XOM", "JNJ", "PG", "KO", "WMT", "HD", "UNH", "V", "COST"]

GRIDS = {
    "rsi_reversion": [{"rsi_buy": b, "exit_sma": e} for b in (5, 10, 15) for e in (5, 10)],
    "sma_trend": [{"sma": n} for n in (50, 100, 200)],
    "ema_pullback": [{"pull_pct": p} for p in (0.5, 1.0, 2.0)],
    "ema_trend": [{"ema": n} for n in (100, 200)],
}


class ValidateError(Exception):
    pass


# ---------- indicators ----------

def sma(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs, n):
    out, k, e = [None] * len(xs), 2.0 / (n + 1), None
    for i, x in enumerate(xs):
        e = x if e is None else x * k + e * (1 - k)
        if i >= n - 1:
            out[i] = e
    return out


def rsi(xs, n=2):
    """Wilder's RSI."""
    out = [None] * len(xs)
    if len(xs) <= n:
        return out
    gains = [max(xs[i] - xs[i - 1], 0) for i in range(1, len(xs))]
    losses = [max(xs[i - 1] - xs[i], 0) for i in range(1, len(xs))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(xs)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


# ---------- strategies: a position (0/1) decided at each close, held the NEXT day ----------

def positions(closes, strategy, p):
    n = len(closes)
    pos = [0] * n
    if strategy == "rsi_reversion":
        r, s200, ex = rsi(closes, 2), sma(closes, 200), sma(closes, p["exit_sma"])
        held = 0
        for i in range(n):
            if held:
                if ex[i] is not None and closes[i] > ex[i]:
                    held = 0
            elif r[i] is not None and s200[i] is not None and r[i] < p["rsi_buy"] and closes[i] > s200[i]:
                held = 1
            pos[i] = held
    elif strategy == "sma_trend":
        s = sma(closes, p["sma"])
        for i in range(n):
            pos[i] = 1 if s[i] is not None and closes[i] > s[i] else 0
    elif strategy == "ema_pullback":
        # the EMA 50/200 post, long side: 50 above 200, buy a pullback to the 50, quit on a close under the 200
        e50, e200 = ema(closes, 50), ema(closes, 200)
        held = 0
        for i in range(n):
            if e200[i] is None:
                continue
            if held:
                if closes[i] < e200[i]:
                    held = 0
            elif e50[i] > e200[i] and e200[i] < closes[i] <= e50[i] * (1 + p["pull_pct"] / 100.0):
                held = 1
            pos[i] = held
    elif strategy == "ema_trend":
        e = ema(closes, p["ema"])
        for i in range(n):
            pos[i] = 1 if e[i] is not None and closes[i] > e[i] else 0
    elif strategy == "buy_hold":
        pos = [1] * n
    else:
        raise ValidateError("unknown strategy %s" % strategy)
    return pos


def daily_returns(closes, pos, start, end):
    """Strategy returns for days start..end-1 (index of the day earned), costs on each change.
    pos[i-1] (decided at close i-1) earns day i: no look-ahead."""
    out, trades = [], []
    entry = None
    for i in range(max(start, 1), end):
        prev = pos[i - 2] if i >= 2 else 0
        held = pos[i - 1]
        r = held * (closes[i] / closes[i - 1] - 1)
        if held != prev:
            r -= COST
            if held:
                entry = closes[i - 1]
            elif entry is not None:
                trades.append(closes[i - 1] / entry - 1 - 2 * COST)
                entry = None
        out.append(r)
    if entry is not None and pos[end - 2 if end >= 2 else 0]:
        trades.append(closes[end - 1] / entry - 1 - 2 * COST)
    return out, trades


def stats(rets):
    n = len(rets)
    if n < 2:
        return {"sharpe": 0.0, "max_dd_pct": 0.0, "cagr_pct": 0.0, "exposure": 0.0}
    m = sum(rets) / n
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (n - 1))
    eq, peak, dd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    years = n / 252.0
    return {"sharpe": round(m / sd * math.sqrt(252), 3) if sd > 0 else 0.0,
            "max_dd_pct": round(dd * 100, 2),
            "cagr_pct": round((eq ** (1 / years) - 1) * 100, 2) if eq > 0 else -100.0,
            "total_pct": round((eq - 1) * 100, 2)}


def bootstrap_sharpe(rets, n=BOOT, seed=0, block=20):
    """Block bootstrap of daily returns; returns the 5th-percentile annual Sharpe."""
    if len(rets) < block * 3:
        return None
    rng = random.Random(seed)
    sh = []
    nb = len(rets) // block
    for _ in range(n):
        sample = []
        for _ in range(nb):
            k = rng.randrange(0, len(rets) - block)
            sample.extend(rets[k:k + block])
        sh.append(stats(sample)["sharpe"])
    sh.sort()
    return round(sh[int(0.05 * len(sh))], 3)


def random_entries(closes, pos, start, end, seed=0, runs=50):
    """Same days in the market as the strategy, placed at random: median Sharpe."""
    days = sum(pos[i - 1] for i in range(max(start, 1), end))
    span = list(range(max(start, 1), end))
    if not span or days == 0:
        return None
    rng = random.Random(seed)
    out = []
    for _ in range(runs):
        pick = set(rng.sample(span, min(days, len(span))))
        rets = [(closes[i] / closes[i - 1] - 1) if i in pick else 0.0 for i in span]
        out.append(stats(rets)["sharpe"])
    out.sort()
    return out[len(out) // 2]


# ---------- walk-forward ----------

def year_index(dates):
    years = {}
    for i, d in enumerate(dates):
        years.setdefault(d[:4], []).append(i)
    return [(y, idx[0], idx[-1] + 1) for y, idx in sorted(years.items())]


def walk_forward(dates, closes, strategy, train_years=TRAIN_YEARS):
    """Pick settings on the train years only, run them on the next year. Returns the record."""
    grid = GRIDS.get(strategy, [{}])
    yrs = year_index(dates)
    cache = {json.dumps(p, sort_keys=True): positions(closes, strategy, p) for p in grid}
    oos, is_rets, chosen, oos_trades = [], [], [], []
    for k in range(train_years, len(yrs)):
        tr_s, tr_e = yrs[k - train_years][1], yrs[k - 1][2]
        te_y, te_s, te_e = yrs[k]
        if te_e - te_s < 100:          # skip a stub final year
            continue
        best, best_sh, best_rets = None, None, None
        for p in grid:
            pos = cache[json.dumps(p, sort_keys=True)]
            r, _ = daily_returns(closes, pos, tr_s, tr_e)
            sh = stats(r)["sharpe"]
            if best_sh is None or sh > best_sh:
                best, best_sh, best_rets = p, sh, r
        pos = cache[json.dumps(best, sort_keys=True)]
        r, t = daily_returns(closes, pos, te_s, te_e)
        oos.extend(r)
        oos_trades.extend(t)
        is_rets.extend(best_rets)
        chosen.append({"test_year": te_y, "settings": best, "train_sharpe": best_sh,
                       "test_sharpe": stats(r)["sharpe"]})
    return {"oos": oos, "is": is_rets, "trades": oos_trades, "chosen": chosen}


def gauntlet(dates, closes, strategy, symbol="?"):
    wf = walk_forward(dates, closes, strategy)
    if not wf["chosen"]:
        raise ValidateError("%s: need at least %d full years of daily history plus one to test"
                            % (symbol, TRAIN_YEARS))
    first_test = dates.index(wf["chosen"][0]["test_year"] + "-01-01") if \
        (wf["chosen"][0]["test_year"] + "-01-01") in dates else \
        next(i for i, d in enumerate(dates) if d[:4] == wf["chosen"][0]["test_year"])
    end = len(dates)
    oos = stats(wf["oos"])
    ins = stats(wf["is"])
    boot = bootstrap_sharpe(wf["oos"])
    n_tr = len(wf["trades"])
    wins = sum(1 for t in wf["trades"] if t > 0)
    bh, _ = daily_returns(closes, positions(closes, "buy_hold", {}), first_test, end)
    t200, _ = daily_returns(closes, positions(closes, "sma_trend", {"sma": 200}), first_test, end)
    last_pos = positions(closes, strategy, wf["chosen"][-1]["settings"])
    rnd = random_entries(closes, last_pos, first_test, end)
    checks = {
        "sharpe": oos["sharpe"] > MIN_SHARPE,
        "drawdown": oos["max_dd_pct"] < MAX_DD,
        "trades": n_tr >= MIN_TRADES,
        "not_overfit": ins["sharpe"] <= 0 or oos["sharpe"] >= DECAY * ins["sharpe"],
        "bootstrap": boot is not None and boot > 0,
    }
    fails = [k for k, v in checks.items() if not v]
    return {
        "symbol": symbol, "strategy": strategy,
        "test_years": "%s-%s" % (wf["chosen"][0]["test_year"], wf["chosen"][-1]["test_year"]),
        "out_of_sample": oos, "in_sample_sharpe": ins["sharpe"],
        "bootstrap_p5_sharpe": boot, "trades": n_tr,
        "win_rate_pct": round(100.0 * wins / n_tr, 1) if n_tr else None,
        "baselines": {"buy_hold": stats(bh), "sma200": stats(t200), "random_same_exposure_sharpe": rnd},
        "beats_buy_hold_sharpe": oos["sharpe"] > stats(bh)["sharpe"],
        "checks": checks, "verdict": "PASS" if not fails else "FAIL", "failed": fails,
        "settings_by_year": wf["chosen"],
        "current_settings": wf["chosen"][-1]["settings"],
    }


def _series(closes):
    rows = sorted((str(d)[:10], float(c)) for d, c in closes if c and float(c) > 0)
    return [d for d, _ in rows], [c for _, c in rows]


def main(argv=None):
    ap = argparse.ArgumentParser(description="The strategy gauntlet (trader-validate).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("symbols", nargs="*")
    r.add_argument("--universe", choices=["default"])
    r.add_argument("--strategy", default="rsi_reversion", choices=sorted(GRIDS))
    r.add_argument("--range", default="15y")
    r.add_argument("--closes", help="offline file {\"closes\": {SYM: [[date, close], ...]}}")
    r.add_argument("--out", default=OUT_PATH)
    r.add_argument("--json", action="store_true")
    r.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    syms = [s.upper() for s in a.symbols] or (DEFAULT_UNIVERSE if a.universe else [])
    if not syms:
        print("validate.py: name symbols or --universe default", file=sys.stderr)
        return 1
    offline = None
    if a.closes:
        with open(a.closes, encoding="utf-8") as fh:
            doc = json.load(fh)
        offline = doc.get("closes", doc)
    results, errors = [], []
    for s in syms:
        try:
            if offline is not None:
                raw = offline.get(s)
                if not raw:
                    raise ValidateError("%s: not in %s" % (s, a.closes))
            else:
                import correlation
                raw = correlation.fetch_daily(s, a.range)
                time.sleep(0.5)
            dates, closes = _series(raw)
            results.append(gauntlet(dates, closes, a.strategy, s))
        except (ValidateError, RuntimeError, ValueError, StopIteration) as e:
            errors.append(str(e))
    passed = [x["symbol"] for x in results if x["verdict"] == "PASS"]
    doc = {"written": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "strategy": a.strategy,
           "rules": {"min_oos_sharpe": MIN_SHARPE, "max_dd_pct": MAX_DD, "min_trades": MIN_TRADES,
                     "oos_vs_is": DECAY, "bootstrap_runs": BOOT, "cost_per_side": COST,
                     "train_years": TRAIN_YEARS},
           "passed": passed, "results": results, "errors": errors}
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    print("%s, walk-forward, costs %.2f%% a side. Out-of-sample only:" % (a.strategy, COST * 100))
    print("%-6s %-9s %6s %6s %7s %6s %6s %6s %6s  %s" % ("sym", "years", "sharpe", "maxDD%", "trades",
                                                          "win%", "boot5", "B&H", "200d", "verdict"))
    for x in results:
        o, b = x["out_of_sample"], x["baselines"]
        print("%-6s %-9s %6.2f %6.1f %7d %6s %6s %6.2f %6.2f  %s%s" % (
            x["symbol"], x["test_years"], o["sharpe"], o["max_dd_pct"], x["trades"],
            "-" if x["win_rate_pct"] is None else "%.0f" % x["win_rate_pct"],
            "-" if x["bootstrap_p5_sharpe"] is None else "%.2f" % x["bootstrap_p5_sharpe"],
            b["buy_hold"]["sharpe"], b["sma200"]["sharpe"], x["verdict"],
            "" if not x["failed"] else " (" + ", ".join(x["failed"]) + ")"))
    for e in errors:
        print("  ! " + e)
    print("passed: %s" % (", ".join(passed) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
