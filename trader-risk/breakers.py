#!/usr/bin/env python3
"""Circuit breakers for trader-risk. Standard library only, no network.

The per-trade checks in check.py keep each loss small. The breakers watch the
ACCOUNT: when the whole account falls far enough, new entries shrink or stop,
whatever the next trade looks like.

They read an equity log, trader/state/equity.json: one account value per
snapshot, oldest first, written by `breakers.py record` (from Alpaca's
account equity, or by hand):

    {"snapshots": [{"time": "2026-10-19T14:30:00Z", "date": "2026-10-19", "equity": 5000.0}, ...]}

The rules (each a percent of the account; 0 or null switches it off):

  day halve     today's equity is breaker_day_halve_pct below yesterday's last
                value (default 2): every entry's max loss is cut in half
  day stop      breaker_day_stop_pct below yesterday's last (default 3): no new
                entries for the rest of the day, and the report says flatten
  week halve    breaker_week_halve_pct below the last value before this week
                (default 5): max loss cut in half
  peak halt     breaker_peak_halt_pct below the highest value ever logged
                (default 10): the bot HALTS. It writes trader/state/HALTED and
                blocks every entry until a person deletes that file by hand.

Exits are never blocked: getting out must always be possible.

    python3 breakers.py record --equity 4890.12          # log a snapshot (now)
    python3 breakers.py status                           # what the breakers say
    python3 breakers.py status --json
"""

import argparse
import datetime
import json
import os
import sys

DEFAULTS = {
    "breaker_day_halve_pct": 2,
    "breaker_day_stop_pct": 3,
    "breaker_week_halve_pct": 5,
    "breaker_peak_halt_pct": 10,
}
EQUITY_PATH = "trader/state/equity.json"
HALT_PATH = "trader/state/HALTED"


class BreakerError(Exception):
    pass


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _pct(cfg, key):
    v = _f(cfg.get(key, DEFAULTS[key]))
    return v if v and v > 0 else None


def snapshots(doc):
    """[(date 'YYYY-MM-DD', equity float)] in file order, bad rows skipped."""
    rows = doc.get("snapshots", doc) if isinstance(doc, dict) else (doc or [])
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        d, e = str(r.get("date") or "")[:10], _f(r.get("equity"))
        if len(d) == 10 and e is not None and e > 0:
            out.append((d, e))
    return out


def _week_start(day):
    d = datetime.date.fromisoformat(day)
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def evaluate(equity_doc, today, cfg=None, halted=None):
    """Pure. What the breakers say for `today` (YYYY-MM-DD, New York).

    halted   the text of the HALTED lock file, or None when there is none.
    Returns {"level": "ok" | "halve" | "stop" | "halt", "factor": 1.0 | 0.5 | 0.0,
             "reasons": [...], "flatten": bool, "trip_halt": bool, "numbers": {...}}"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    rows = snapshots(equity_doc)
    out = {"level": "ok", "factor": 1.0, "reasons": [], "flatten": False, "trip_halt": False, "numbers": {}}
    if halted is not None:
        why = halted.strip().splitlines()[0] if halted.strip() else "no reason recorded"
        out.update(level="halt", factor=0.0)
        out["reasons"].append("halt: the bot is halted (%s). A person must review what happened and delete "
                              "trader/state/HALTED to restart it" % why)
        return out
    upto = [(d, e) for d, e in rows if d <= today]
    if not upto:
        return out
    now = upto[-1][1]
    before = [e for d, e in upto if d < today]
    week = [e for d, e in upto if d < _week_start(today)]
    peak = max(e for _, e in upto)
    n = out["numbers"]
    n["equity"] = round(now, 2)
    n["peak"] = round(peak, 2)
    n["from_peak_pct"] = round((now / peak - 1) * 100, 2)
    if before:
        n["yesterday"] = round(before[-1], 2)
        n["day_pct"] = round((now / before[-1] - 1) * 100, 2)
    if week:
        n["last_week"] = round(week[-1], 2)
        n["week_pct"] = round((now / week[-1] - 1) * 100, 2)

    def drop(key):
        v = n.get(key)
        return -v if v is not None and v < 0 else 0.0

    halt_p = _pct(c, "breaker_peak_halt_pct")
    if halt_p and drop("from_peak_pct") >= halt_p - 1e-9:
        out.update(level="halt", factor=0.0, trip_halt=True, flatten=True)
        out["reasons"].append("halt: the account is %.2f%% below its peak (%.2f to %.2f), at or past the %s%% "
                              "halt. Writing trader/state/HALTED: nothing new trades until a person deletes it"
                              % (drop("from_peak_pct"), peak, now, _num(halt_p)))
        return out
    stop_p = _pct(c, "breaker_day_stop_pct")
    if stop_p and drop("day_pct") >= stop_p - 1e-9:
        out.update(level="stop", factor=0.0, flatten=True)
        out["reasons"].append("day stop: the account is down %.2f%% today, at or past %s%%: no new entries "
                              "today, flatten what is open" % (drop("day_pct"), _num(stop_p)))
        return out
    halves = []
    day_p = _pct(c, "breaker_day_halve_pct")
    if day_p and drop("day_pct") >= day_p - 1e-9:
        halves.append("day: down %.2f%% today (halve at %s%%)" % (drop("day_pct"), _num(day_p)))
    week_p = _pct(c, "breaker_week_halve_pct")
    if week_p and drop("week_pct") >= week_p - 1e-9:
        halves.append("week: down %.2f%% this week (halve at %s%%)" % (drop("week_pct"), _num(week_p)))
    if halves:
        out.update(level="halve", factor=0.5)
        out["reasons"].append("halve: max loss per trade cut in half (%s)" % "; ".join(halves))
    return out


def _num(x):
    return ("%d" % x) if float(x).is_integer() else ("%s" % x)


def read_doc(path):
    if not os.path.exists(path):
        return {"snapshots": []}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        raise BreakerError("cannot read %s: %s" % (path, e))


def read_halt(path=HALT_PATH):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write_halt(reason, path=HALT_PATH, when=None):
    """Create the lock file once. Never overwrite one a person has not cleared."""
    if os.path.exists(path):
        return False
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    when = when or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("%s (tripped %s)\n" % (reason, when))
        fh.write("Review the trade log and the equity log, then delete this file by hand to restart.\n")
    return True


def record(path, equity, date, time):
    doc = read_doc(path)
    rows = doc.get("snapshots") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        rows = []
    rows.append({"time": time, "date": date, "equity": round(equity, 2)})
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"snapshots": rows}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return len(rows)


def _ny_today():
    # same New York date rule check.py uses
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import check  # noqa: E402
    now = check.parse_now(None)
    return check.to_eastern(now).date().isoformat(), now.strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Account circuit breakers (trader-risk).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="log one equity snapshot")
    r.add_argument("--equity", type=float, required=True)
    r.add_argument("--date", help="YYYY-MM-DD, New York (default: today)")
    s = sub.add_parser("status", help="what the breakers say now")
    s.add_argument("--today")
    s.add_argument("--json", action="store_true")
    for p in (r, s):
        p.add_argument("--file", default=EQUITY_PATH)
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--halt-file", default=HALT_PATH)
    a = ap.parse_args(argv)
    try:
        today, stamp = _ny_today()
        if a.cmd == "record":
            if a.equity <= 0:
                raise BreakerError("--equity must be above 0")
            n = record(a.file, a.equity, a.date or today, stamp)
            print("logged equity %.2f for %s (%d snapshots)" % (a.equity, a.date or today, n))
            return 0
        cfg = {}
        if os.path.exists(a.config):
            with open(a.config, encoding="utf-8") as fh:
                cfg = json.load(fh)
        res = evaluate(read_doc(a.file), a.today or today, cfg, read_halt(a.halt_file))
        if res["trip_halt"]:
            write_halt(res["reasons"][0], a.halt_file)
    except (BreakerError, ValueError, OSError) as e:
        print("breakers.py: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print("breakers: %s" % res["level"].upper())
        for k, v in res["numbers"].items():
            print("  %s: %s" % (k, v))
        for x in res["reasons"]:
            print("  - " + x)
    return 0 if res["level"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
