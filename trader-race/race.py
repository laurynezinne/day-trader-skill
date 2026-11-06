#!/usr/bin/env python3
"""The £200 race (trader-race). Paper only, simulated from real daily closes.

Two bots start with £200 each and compound every trading day:

  safe     the validated rsi_reversion strategy on the gauntlet's passes
           (SPY, MSFT, NVDA, GOOGL, V at their walk-forward settings), 20% of
           the account per signal, scaled by the market type (crash = cash)
  all_in   chases 3.44% a day: at each close, 100% into whichever 3x
           leveraged ETF rose most over the last 3 days (TQQQ, SOXL, UPRO,
           TNA, TECL). No brakes. It cannot fall below £0.

Decisions are made at a close and earn the NEXT day's move. Every change of
holding costs 0.05%. Returns are the US dollar % moves applied to a £ balance
(currency moves are ignored). Scoreboard lines: £200 compounding at 3.44% a
day (£1M in a year), at 5% a day (8 months), and SPY held.

    python3 race.py start --date 2026-11-05     # day 0 at that close
    python3 race.py step                         # catch up to the latest close
    python3 race.py show
"""

import argparse
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for d in ("trader-risk", "trader-regime", "trader-validate"):
    sys.path.insert(0, os.path.join(HERE, "..", d))

import validate  # noqa: E402

STATE_PATH = os.path.join(HERE, "race.json")
START_GBP = 200.0
COST = 0.0005
SAFE_WEIGHT = 0.2
SAFE = {"SPY": {"rsi_buy": 10, "exit_sma": 5}, "MSFT": {"rsi_buy": 15, "exit_sma": 5},
        "NVDA": {"rsi_buy": 10, "exit_sma": 5}, "GOOGL": {"rsi_buy": 5, "exit_sma": 5},
        "V": {"rsi_buy": 10, "exit_sma": 10}}            # trader-validate, 5 Nov 2026
ALL_IN = ["TQQQ", "SOXL", "UPRO", "TNA", "TECL"]
LOOKBACK = 3
TARGETS = {"target_1y": 0.0344, "target_8m": 0.05}
FACTORS = {"crash": 0.0, "bear": 0.5, "neutral": 0.75, "bull": 1.0, "euphoria": 0.75}


class RaceError(Exception):
    pass


def symbols():
    return sorted(set(SAFE) | set(ALL_IN) | {"SPY"})


def table(closes):
    """{SYM: [[date, close]]} -> (dates common to all, {SYM: {date: close}})."""
    by = {s: {str(d)[:10]: float(c) for d, c in rows if c} for s, rows in closes.items()}
    common = sorted(set.intersection(*(set(v) for v in by.values())))
    return common, by


def safe_weights(by, dates_upto, regime_name):
    """Target weights at the close of dates_upto[-1], from data up to then only."""
    f = FACTORS.get(regime_name or "", 1.0)
    w = {}
    for sym, p in SAFE.items():
        closes = [by[sym][d] for d in dates_upto]
        if validate.positions(closes, "rsi_reversion", p)[-1]:
            w[sym] = SAFE_WEIGHT * f
    return {k: v for k, v in w.items() if v > 0}


def all_in_weights(by, dates_upto):
    if len(dates_upto) <= LOOKBACK:
        return {}
    d0, d1 = dates_upto[-1 - LOOKBACK], dates_upto[-1]
    best = max(ALL_IN, key=lambda s: by[s][d1] / by[s][d0])
    return {best: 1.0}


def day_return(w, by, prev, cur):
    return sum(x * (by[s][cur] / by[s][prev] - 1) for s, x in w.items())


def turnover(old, new):
    keys = set(old) | set(new)
    return sum(abs(new.get(k, 0) - old.get(k, 0)) for k in keys)


def drift(w, by, prev, cur):
    """Weights after a day's move (cash takes the rest)."""
    if not w:
        return {}
    grown = {s: x * by[s][cur] / by[s][prev] for s, x in w.items()}
    total = sum(grown.values()) + (1 - sum(w.values()))
    return {s: v / total for s, v in grown.items()} if total > 0 else {}


def regime_for(by, dates_upto):
    """Market type at this close, refit on SPY data up to it (forward pass only)."""
    import regime as rg
    rows = [[d, by["SPY"][d]] for d in dates_upto]
    dts, X = rg.features(rows)
    model, _ = rg.select(X, states=(3, 4, 5), seeds=(0,))
    return rg.classify(dts, X, model)["regime"]


def start(closes, day0, regime_fn=regime_for):
    dates, by = table(closes)
    if day0 not in dates:
        raise RaceError("%s is not a trading day in the data" % day0)
    upto = dates[:dates.index(day0) + 1]
    reg = regime_fn(by, upto)
    bots = {
        "safe": {"gbp": START_GBP, "weights": safe_weights(by, upto, reg)},
        "all_in": {"gbp": START_GBP, "weights": all_in_weights(by, upto)},
        "spy_hold": {"gbp": START_GBP, "weights": {"SPY": 1.0}},
    }
    for b in bots.values():   # entering costs too
        b["gbp"] = round(b["gbp"] * (1 - COST * turnover({}, b["weights"])), 4)
    return {"start_date": day0, "last_date": day0, "start_gbp": START_GBP, "regime": reg,
            "bots": bots, "days": [{"date": day0, "day": 0, "regime": reg,
                                    **{k: {"gbp": round(v["gbp"], 2), "ret_pct": 0.0,
                                           "next": v["weights"]} for k, v in bots.items()},
                                    **{k: START_GBP for k in TARGETS}}]}


def step(state, closes, regime_fn=regime_for, until=None):
    """Advance through every close after last_date (catches up missed days)."""
    dates, by = table(closes)
    todo = [d for d in dates if d > state["last_date"] and (until is None or d <= until)]
    for cur in todo:
        i = dates.index(cur)
        prev = dates[i - 1]
        upto = dates[:i + 1]
        reg = regime_fn(by, upto)
        rec = {"date": cur, "day": len(state["days"]), "regime": reg}
        n = rec["day"]
        for name, b in state["bots"].items():
            r = day_return(b["weights"], by, prev, cur)
            held = drift(b["weights"], by, prev, cur)
            if name == "safe":
                new = safe_weights(by, upto, reg)
            elif name == "all_in":
                new = all_in_weights(by, upto)
            else:
                new = held
            gbp = max(0.0, b["gbp"] * (1 + r))
            gbp *= 1 - COST * turnover(held, new)
            b["gbp"], b["weights"] = round(gbp, 4), new
            rec[name] = {"gbp": round(gbp, 2), "ret_pct": round(r * 100, 2), "next": new}
        for k, g in TARGETS.items():
            rec[k] = round(START_GBP * (1 + g) ** n, 2)
        state["days"].append(rec)
        state["last_date"], state["regime"] = cur, reg
    return state, todo


def fetch_all(rng="2y"):
    import correlation
    out = {}
    for s in symbols():
        out[s] = correlation.fetch_daily(s, rng)
        time.sleep(0.4)
    return out


def show(state):
    last = state["days"][-1]
    lines = ["£200 race: day %d (%s), market type %s" % (last["day"], last["date"], last["regime"])]
    for k, label in (("safe", "Safe bot"), ("all_in", "All-in bot"), ("spy_hold", "SPY held"),
                     ("target_1y", "£1M-in-1-year line"), ("target_8m", "8-month line")):
        v = last[k]["gbp"] if isinstance(last[k], dict) else last[k]
        lines.append("  %-20s £%9.2f" % (label, v))
    for k in ("safe", "all_in"):
        nxt = last[k]["next"]
        lines.append("  %s holds next: %s" % (k, ", ".join("%s %.0f%%" % (s, w * 100) for s, w in nxt.items())
                                               or "cash"))
    return "\n".join(lines)


def rows(state):
    """Flat rows for the dashboard: one per trading day."""
    out = []
    for d in state["days"]:
        nxt = lambda k: ", ".join("%s %.0f%%" % (s, w * 100) for s, w in d[k]["next"].items()) or "cash"
        out.append({"date": d["date"], "day": d["day"], "regime": d["regime"],
                    "safe": d["safe"]["gbp"], "safe_ret": d["safe"]["ret_pct"],
                    "all_in": d["all_in"]["gbp"], "all_in_ret": d["all_in"]["ret_pct"],
                    "spy_hold": d["spy_hold"]["gbp"], "spy_ret": d["spy_hold"]["ret_pct"],
                    "target_1y": d["target_1y"], "target_8m": d["target_8m"],
                    "safe_next": nxt("safe"), "all_in_next": nxt("all_in")})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="The £200 race (paper simulation).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--date", required=True)
    sub.add_parser("step")
    sub.add_parser("show")
    e = sub.add_parser("export", help="write the dashboard's rows file")
    e.add_argument("--out", default=os.path.join(HERE, "race-rows.json"))
    for p in sub.choices.values():
        p.add_argument("--state", default=STATE_PATH)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "start":
            if os.path.exists(a.state):
                raise RaceError("%s exists: the race has started" % a.state)
            st = start(fetch_all(), a.date)
        else:
            with open(a.state, encoding="utf-8") as fh:
                st = json.load(fh)
            if a.cmd == "export":
                with open(a.out, "w", encoding="utf-8") as fh:
                    json.dump({"rows": rows(st)}, fh, indent=1)
                    fh.write("\n")
                print("wrote %d rows to %s" % (len(st["days"]), a.out))
                return 0
            if a.cmd == "step":
                st, done = step(st, fetch_all())
                print("advanced %d day(s): %s" % (len(done), ", ".join(done) or "none (no new close yet)"))
        if a.cmd in ("start", "step"):
            with open(a.state, "w", encoding="utf-8") as fh:
                json.dump(st, fh, indent=1)
                fh.write("\n")
        print(show(st))
    except (RaceError, RuntimeError, OSError, ValueError) as e:
        print("race.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
