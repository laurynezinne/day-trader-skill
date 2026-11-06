#!/usr/bin/env python3
"""Markets file for the commodities and crypto dashboard (trader-regime).

For each asset: about a year of daily closes (Yahoo, free and unofficial)
and today's market type from regime.py (forward pass only). Price changes are
NOT worked out here: the dashboard computes them from the closes, so a reader
can check every number.

    python3 markets.py --out markets.json
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "trader-risk"))

import regime       # noqa: E402
import correlation  # noqa: E402

ASSETS = [
    ("GC=F", "Gold", "Metals", "$/oz"),
    ("SI=F", "Silver", "Metals", "$/oz"),
    ("PL=F", "Platinum", "Metals", "$/oz"),
    ("HG=F", "Copper", "Metals", "$/lb"),
    ("CL=F", "Oil (WTI)", "Energy", "$/barrel"),
    ("BZ=F", "Oil (Brent)", "Energy", "$/barrel"),
    ("NG=F", "Natural gas", "Energy", "$/MMBtu"),
    ("BTC-USD", "Bitcoin", "Crypto", "$"),
    ("ETH-USD", "Ethereum", "Crypto", "$"),
    ("SOL-USD", "Solana", "Crypto", "$"),
    ("XRP-USD", "XRP", "Crypto", "$"),
]
KEEP_DAYS = 260


def build(fetch=None, pause=0.4):
    if fetch is None:
        fetch = lambda s: correlation.fetch_daily(s, "5y")  # noqa: E731
    else:
        pause = 0
    rows, errors = [], []
    for sym, name, group, unit in ASSETS:
        try:
            hist = fetch(sym)
            dates, X = regime.features(hist)
            model, _ = regime.select(X, states=(3, 4, 5), seeds=(0,))
            res = regime.classify(dates, X, model)
            rows.append({"symbol": sym, "name": name, "group": group, "unit": unit,
                         "asof": hist[-1][0], "regime": res["regime"],
                         "regime_watch": res["most_likely_today"] if res["pending_change"] else "",
                         "closes": hist[-KEEP_DAYS:]})
        except Exception as e:  # noqa: BLE001  one bad symbol never stops the rest
            errors.append("%s: %s" % (sym, e))
        time.sleep(pause)
    return {"written": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rows": rows, "errors": errors}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Markets file for the dashboard.")
    ap.add_argument("--out", default="markets.json")
    a = ap.parse_args(argv)
    doc = build()
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    print("%d assets, %d errors -> %s" % (len(doc["rows"]), len(doc["errors"]), a.out))
    for r in doc["rows"]:
        print("  %-12s %s %10.4f  %s%s" % (r["name"], r["asof"], r["closes"][-1][1], r["regime"],
                                          (" (watching " + r["regime_watch"] + ")") if r["regime_watch"] else ""))
    for e in doc["errors"]:
        print("  ! " + e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
