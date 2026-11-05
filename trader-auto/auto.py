#!/usr/bin/env python3
"""Automatic PAPER runner (trader-auto). Standard library only.

One run, meant for once a day about 15:20 New York time (20:20 UK):

  1. refuse unless the config says "auto": true AND "paper": true, and the
     broker is Alpaca's PAPER endpoint (hard-coded; there is no live URL here)
  2. log the account's equity; evaluate the circuit breakers
     (a halt or a day stop flattens everything and stops)
  3. read today's market type (trader-regime) and the gauntlet's passes
     (trader-validate); only symbols that PASSED are ever traded
  4. for each passed symbol, the validated strategy at its current settings
     says hold or not, from daily closes plus today's price
  5. exits first: anything held that the strategy no longer wants is sold
  6. entries: a bracket order (stop 2 x the 20-day daily move below, target
     1.5 x that distance above), sized to the max loss, then put through
     trader-risk's full check (breakers, market type, correlation, limits).
     Only PASS orders are sent.
  7. every order goes in trader/state/broker.json; every run gets a plain
     line in trader/journal/auto-YYYY-MM.md, the bot's memory

    python3 auto.py run --dry-run     # everything except sending orders
    python3 auto.py run               # paper orders, if every check allows
    python3 auto.py status

Keys come from the environment only: APCA_API_KEY_ID and APCA_API_SECRET_KEY
(paper keys from app.alpaca.markets, Paper Trading). Never from a file in
this repo, never from the chat.
"""

import argparse
import datetime
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
for d in ("trader-risk", "trader-regime", "trader-validate"):
    sys.path.insert(0, os.path.join(HERE, "..", d))

import breakers     # noqa: E402
import check        # noqa: E402
import correlation  # noqa: E402
import validate     # noqa: E402

PAPER_URL = "https://paper-api.alpaca.markets"
STATE = "trader/state/broker.json"
JOURNAL_DIR = "trader/journal"
STOP_SIGMA = 2.0
TARGET_R = 1.5
MIN_STOP_PCT = 0.02


class AutoError(Exception):
    pass


# ---------- broker ----------

class AlpacaPaper:
    """Alpaca's paper trading REST API. The URL is fixed: this class cannot reach a live account."""

    def __init__(self, key=None, secret=None, timeout=20):
        self.key = key or os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_API_KEY")
        self.secret = secret or os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY")
        if not self.key or not self.secret:
            raise AutoError("no Alpaca paper keys: set APCA_API_KEY_ID and APCA_API_SECRET_KEY in the "
                            "environment (never in a file in this repo, never in the chat)")
        self.timeout = timeout

    def _req(self, method, path, body=None):
        url = PAPER_URL + path
        assert url.startswith(PAPER_URL + "/"), "paper endpoint only"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise AutoError("Alpaca %s %s: HTTP %s %s" % (method, path, e.code, e.read()[:300]))
        return json.loads(raw) if raw else {}

    def clock(self):
        return self._req("GET", "/v2/clock")

    def account(self):
        return self._req("GET", "/v2/account")

    def positions(self):
        return self._req("GET", "/v2/positions")

    def open_orders(self, symbol):
        return self._req("GET", "/v2/orders?status=open&symbols=%s" % urllib.request.quote(symbol))

    def cancel(self, order_id):
        return self._req("DELETE", "/v2/orders/%s" % order_id)

    def close_position(self, symbol):
        return self._req("DELETE", "/v2/positions/%s" % urllib.request.quote(symbol))

    def close_all(self):
        return self._req("DELETE", "/v2/positions?cancel_orders=true")

    def submit(self, order):
        return self._req("POST", "/v2/orders", order)


# ---------- helpers ----------

def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def daily_sigma(closes, n=20):
    rs = [closes[i] / closes[i - 1] - 1 for i in range(max(1, len(closes) - n), len(closes))]
    if len(rs) < 5:
        return None
    m = sum(rs) / len(rs)
    return math.sqrt(sum((r - m) ** 2 for r in rs) / (len(rs) - 1))


def bracket_for(sym, price, closes, max_loss, max_order):
    """Shares, stop and target for one entry. None when the stop distance or size makes no sense."""
    sig = daily_sigma(closes)
    if not sig or price <= 0:
        return None
    dist = max(STOP_SIGMA * sig, MIN_STOP_PCT) * price
    stop = round(price - dist, 2)
    target = round(price + TARGET_R * dist, 2)
    qty = int(max_loss // dist)
    if max_order:
        qty = min(qty, int(max_order // price))
    if qty < 1 or stop <= 0:
        return None
    return {"symbol": sym, "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "gtc",
            "order_class": "bracket", "take_profit_limit_price": "%.2f" % target,
            "stop_loss_stop_price": "%.2f" % stop,
            "why": "auto: validated %s entry, stop 2x the 20-day daily move" % sym}


def alpaca_order(o):
    return {"symbol": o["symbol"], "qty": o["qty"], "side": o["side"], "type": o["type"],
            "time_in_force": o["time_in_force"], "order_class": "bracket",
            "take_profit": {"limit_price": o["take_profit_limit_price"]},
            "stop_loss": {"stop_price": o["stop_loss_stop_price"]}}


def journal(lines, now_et, jdir=JOURNAL_DIR):
    os.makedirs(jdir, exist_ok=True)
    path = os.path.join(jdir, "auto-%s.md" % now_et.strftime("%Y-%m"))
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write("# Auto runner journal, %s (paper)\n\n" % now_et.strftime("%B %Y"))
        fh.write("## %s ET\n\n" % now_et.strftime("%Y-%m-%d %H:%M"))
        for ln in lines:
            fh.write("- %s\n" % ln)
        fh.write("\n")
    return path


def log_order(state_path, rec):
    st = read_json(state_path, {"orders": []}) or {"orders": []}
    st.setdefault("orders", []).append(rec)
    write_json(state_path, st)


# ---------- one run ----------

def run(cfg, broker, fetch, now=None, dry_run=False, paths=None):
    """One automatic paper run. `broker` is AlpacaPaper (or a test double), `fetch(sym)` returns
    [[date, close], ...]. Returns the report; never raises after an order is sent."""
    p = {"equity": breakers.EQUITY_PATH, "halt": breakers.HALT_PATH, "regime": "trader/state/regime.json",
         "validation": validate.OUT_PATH, "state": STATE, "journal": JOURNAL_DIR, "risk_out": None}
    p.update(paths or {})
    if not cfg.get("auto"):
        raise AutoError("\"auto\" is not true in the config: the automatic runner is off")
    if not cfg.get("paper", True):
        raise AutoError("\"paper\" is false: the automatic runner only ever trades a paper account")
    now = now or check.parse_now(None)
    now_et = check.to_eastern(now)
    today = now_et.date().isoformat()
    lines, sent, report = [], [], {"date": today, "dry_run": dry_run}

    clock = broker.clock()
    if not clock.get("is_open"):
        lines.append("market closed: nothing to do")
        report["skipped"] = "market closed"
        report["journal"] = journal(lines, now_et, p["journal"])
        return report

    acct = broker.account()
    equity = float(acct.get("equity") or 0)
    cash = float(acct.get("cash") or 0)
    breakers.record(p["equity"], equity, today, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    brk = breakers.evaluate(breakers.read_doc(p["equity"]), today, cfg, breakers.read_halt(p["halt"]))
    if brk["trip_halt"]:
        breakers.write_halt(brk["reasons"][0], p["halt"])
    report["breakers"] = brk["level"]
    lines.append("equity %.2f, breakers %s" % (equity, brk["level"].upper()))

    if brk.get("flatten") or brk["level"] in ("stop", "halt"):
        for r in brk["reasons"]:
            lines.append(r)
        if not dry_run:
            broker.close_all()
        lines.append("%sclosed every position; no new entries" % ("[dry run] would have " if dry_run else ""))
        report["flattened"] = True
        report["journal"] = journal(lines, now_et, p["journal"])
        return report

    val = read_json(p["validation"])
    if not val or not val.get("passed"):
        lines.append("no validated symbols (run trader-validate): nothing to trade")
        report["skipped"] = "nothing validated"
        report["journal"] = journal(lines, now_et, p["journal"])
        return report
    strategy = val["strategy"]
    settings = {r["symbol"]: r["current_settings"] for r in val.get("results", []) if r["verdict"] == "PASS"}
    reg = read_json(p["regime"])
    if reg:
        lines.append("market type %s (as of %s), size factor %s"
                     % (reg.get("regime"), reg.get("date"), reg.get("factor")))

    held = {}
    for pos in broker.positions():
        held[str(pos["symbol"]).upper()] = {"qty": float(pos["qty"]), "price": float(pos.get("current_price") or 0)}

    want, prices, closes_all = {}, {}, {}
    for sym in sorted(settings):
        try:
            hist = fetch(sym)
        except Exception as e:  # noqa: BLE001
            lines.append("%s: no data (%s); skipped" % (sym, e))
            continue
        dates, closes = validate._series(hist)
        if not closes:
            continue
        price = held.get(sym, {}).get("price") or closes[-1]
        if dates and dates[-1] != today:
            dates, closes = dates + [today], closes + [price]
        else:
            closes[-1] = price
        pos = validate.positions(closes, strategy, settings[sym])
        want[sym] = pos[-1]
        prices[sym] = price
        closes_all[sym] = [[d, c] for d, c in zip(dates, closes)]

    # exits first
    for sym, h in sorted(held.items()):
        if sym in want and want[sym] == 0 and h["qty"] > 0:
            lines.append("EXIT %s: the %s rule no longer holds it (%g shares)" % (sym, strategy, h["qty"]))
            if not dry_run:
                try:
                    for o in broker.open_orders(sym):
                        broker.cancel(o["id"])
                    res = broker.close_position(sym)
                    log_order(p["state"], {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "date": today,
                                           "symbol": sym, "side": "sell", "qty": str(h["qty"]),
                                           "kind": "exit", "status": "placed", "auto": True, "paper": True,
                                           "broker_id": res.get("id")})
                    sent.append(sym)
                except AutoError as e:
                    lines.append("  exit failed: %s" % e)

    # entries
    cands = [s for s in sorted(want) if want[s] == 1 and held.get(s, {}).get("qty", 0) <= 0]
    orders = []
    # size to the max loss trader-risk will actually allow today: market type x breakers
    eff_loss = float(cfg.get("max_loss_per_trade_usd") or 0)
    if isinstance(reg, dict) and reg.get("factor") is not None and 0 < float(reg["factor"]) < 1:
        eff_loss *= float(reg["factor"])
    if brk["level"] == "halve":
        eff_loss *= float(brk.get("factor") or 1)
    eff_loss = round(eff_loss, 2)
    if eff_loss != float(cfg.get("max_loss_per_trade_usd") or 0):
        lines.append("sizing to a max loss of $%.2f today (market type and breakers)" % eff_loss)
    for sym in cands:
        o = bracket_for(sym, prices[sym], [c for _, c in closes_all[sym]],
                        eff_loss, float(cfg.get("max_order_usd") or 0))
        if o:
            orders.append(o)
        else:
            lines.append("%s: signal, but no sensible size at max loss %s" % (sym, cfg.get("max_loss_per_trade_usd")))
    if orders:
        portfolio = {"cash": cash, "positions": [{"ticker": s, "shares": h["qty"], "price": h["price"]}
                                                 for s, h in held.items()]}
        state = read_json(p["state"], {"orders": []})
        res = check.check(orders, cfg, portfolio, prices, state, today, now, None, brk, closes_all,
                          reg if isinstance(reg, dict) else None)
        if p.get("risk_out"):
            write_json(p["risk_out"], res)
        for w in res.get("warnings", []):
            lines.append("risk warning: %s" % w)
        for o, r in zip(orders, res["results"]):
            if r["verdict"] != "PASS":
                lines.append("BLOCKED %s: %s" % (o["symbol"], "; ".join(r["reasons"])))
                continue
            plain = "buy %s %s, stop %s, target %s" % (o["qty"], o["symbol"], o["stop_loss_stop_price"],
                                                       o["take_profit_limit_price"])
            if dry_run:
                lines.append("[dry run] would ENTER: " + plain)
                continue
            try:
                ack = broker.submit(alpaca_order(o))
                log_order(p["state"], {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "date": today,
                                       "symbol": o["symbol"], "side": "buy", "qty": o["qty"], "kind": "entry",
                                       "max_loss": r.get("max_loss"), "stop": float(o["stop_loss_stop_price"]),
                                       "status": "placed", "auto": True, "paper": True,
                                       "broker_id": ack.get("id")})
                sent.append(o["symbol"])
                lines.append("ENTER: " + plain)
            except AutoError as e:
                lines.append("  entry failed for %s: %s" % (o["symbol"], e))
    if not cands and not any(l.startswith("EXIT") for l in lines):
        lines.append("no signals today; holding %s" % (", ".join(sorted(held)) or "nothing"))
    report.update(sent=sent, signals=want, journal=journal(lines, now_et, p["journal"]), lines=lines)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="Automatic paper runner (trader-auto).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="do everything except send orders")
    r.add_argument("--config", default="trader/config.json")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print("halted: %s" % ("YES (trader/state/HALTED)" if os.path.exists(breakers.HALT_PATH) else "no"))
        val = read_json(validate.OUT_PATH) or {}
        print("validated: %s (%s)" % (", ".join(val.get("passed") or []) or "none", val.get("strategy", "-")))
        reg = read_json("trader/state/regime.json") or {}
        print("market type: %s (as of %s)" % (reg.get("regime", "-"), reg.get("date", "-")))
        return 0
    try:
        cfg = read_json(a.config)
        if cfg is None:
            raise AutoError("no %s: copy trader-budget/config.example.json there first" % a.config)
        broker = AlpacaPaper()
        rep = run(cfg, broker, lambda s: correlation.fetch_daily(s, "2y"), dry_run=a.dry_run)
    except AutoError as e:
        print("auto.py: %s" % e, file=sys.stderr)
        return 1
    for ln in rep.get("lines", []) or []:
        print(ln)
    print("journal: %s" % rep.get("journal"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
