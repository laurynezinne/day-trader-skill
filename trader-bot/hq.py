#!/usr/bin/env python3
"""Builds the Bot HQ page from bot.json: one self-contained HTML file.

    python3 hq.py --bot bot.json --out bot-hq.html
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
START = 10000.0


def slim(doc):
    t, p = doc["trend"], doc["paper"]
    prev, cal = START, []
    closed = {}
    for tr in p.get("closed", []):
        closed[tr["exit_date"]] = closed.get(tr["exit_date"], 0) + 1
    for d, eq, _hold in p.get("equity_curve", []):
        cal.append({"date": d, "pnl": round(eq - prev, 2), "closed": closed.get(d, 0)})
        prev = eq
    return {
        "written": doc["written"], "markets": doc["markets"], "trend_rules": doc["trend_rules"],
        "trend": {"stats": t["stats"], "calendar": t["calendar"], "equity": t["equity"], "trades": t["trades"]},
        "trend_now": doc["trend_now"],
        "scalper": {"stats": doc["scalper"]["stats"]},
        "scalper_now": [{"symbol": r["symbol"], "score": r["score"], "veto": r["veto"]} for r in doc["scalper_now"]],
        "paper": {"since": p["since"], "equity": p["equity"], "return_pct": p["return_pct"], "open": p["open"],
                  "trades": p["trades"], "calendar": cal},
    }


def build(doc, template):
    data = json.dumps(slim(doc), separators=(",", ":")).replace("</", "<\\/")
    if "/*DATA*/null" not in template:
        raise SystemExit("template has no /*DATA*/null slot")
    return template.replace("/*DATA*/null", data)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the Bot HQ page.")
    ap.add_argument("--bot", default="bot.json")
    ap.add_argument("--out", default="bot-hq.html")
    ap.add_argument("--template", default=os.path.join(HERE, "hq-template.html"))
    a = ap.parse_args(argv)
    with open(a.bot, encoding="utf-8") as fh:
        doc = json.load(fh)
    with open(a.template, encoding="utf-8") as fh:
        tpl = fh.read()
    html = build(doc, tpl)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("%s: %d KB" % (a.out, len(html) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
