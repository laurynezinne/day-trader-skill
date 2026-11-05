#!/usr/bin/env python3
"""Correlation check for trader-risk. Standard library only.

Three stocks that move together are one big bet, not three small ones. Before
a new entry passes, check.py compares the stock's daily returns with every
stock already held (and every entry passed earlier in the same batch). When
the correlation is at or over "max_correlation" (default 0.8) the entry is
blocked.

It reads a closes file, trader/state/closes.json, which `fetch` writes from
Yahoo's free daily bars:

    {"fetched": "2026-11-05T21:10:00Z", "closes": {"AAPL": [["2026-08-01", 218.2], ...], ...}}

    python3 correlation.py fetch AAPL MSFT NVDA          # refresh the closes file
    python3 correlation.py show AAPL MSFT NVDA           # print the correlation grid

Returns are matched by date, so a missing day in one stock never shifts the
other. Fewer than 20 shared days = no verdict (a warning, never a block).
"""

import argparse
import datetime
import json
import math
import os
import sys
import time
import urllib.request

CLOSES_PATH = "trader/state/closes.json"
MIN_DAYS = 20
DEFAULT_MAX = 0.8
DEFAULT_DAYS = 60
UA = "Mozilla/5.0 (day-trader-skill correlation check)"


def returns(series):
    """[(date, close)] -> {date: simple return vs the previous close}."""
    rows = sorted((str(d)[:10], float(c)) for d, c in series if c)
    out = {}
    for (d0, c0), (d1, c1) in zip(rows, rows[1:]):
        if c0 > 0:
            out[d1] = c1 / c0 - 1.0
    return out


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def corr(closes, a, b, days=DEFAULT_DAYS):
    """(r, shared_days) over the last `days` dates both stocks have; r None when too few."""
    ra, rb = returns(closes.get(a) or []), returns(closes.get(b) or [])
    shared = sorted(set(ra) & set(rb))[-days:]
    if len(shared) < MIN_DAYS:
        return None, len(shared)
    return pearson([ra[d] for d in shared], [rb[d] for d in shared]), len(shared)


def conflicts(sym, others, closes, cfg=None):
    """Pure. Returns (blocks, warnings): blocks = [(other, r)] at or over the limit."""
    c = cfg or {}
    lim = c.get("max_correlation", DEFAULT_MAX)
    if lim in (None, 0, False):
        return [], []
    lim = float(lim)
    days = int(c.get("correlation_days", DEFAULT_DAYS) or DEFAULT_DAYS)
    blocks, warns = [], []
    for o in others:
        if o == sym:
            continue
        r, n = corr(closes, sym, o, days)
        if r is None:
            warns.append("correlation: not enough shared daily history for %s vs %s (%d days, need %d); "
                         "refresh with correlation.py fetch %s %s" % (sym, o, n, MIN_DAYS, sym, o))
        elif r >= lim - 1e-9:
            blocks.append((o, round(r, 2)))
    return blocks, warns


def read_closes(path=CLOSES_PATH):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc.get("closes", doc) if isinstance(doc, dict) else None


def fetch_daily(sym, rng="6mo", timeout=20):
    """[(date, close)] from Yahoo's free daily chart. Unofficial; can lag or rate-limit."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d&includeAdjustedClose=true"
           % (urllib.request.quote(sym), rng))
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                doc = json.load(r)
            res = doc["chart"]["result"][0]
            ts = res.get("timestamp") or []
            adj = (res.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")
            cl = adj or res["indicators"]["quote"][0]["close"]
            out = []
            for t, c in zip(ts, cl):
                if c:
                    out.append([datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d"),
                                round(float(c), 4)])
            return out
        except Exception as e:  # noqa: BLE001  network, rate limit, bad symbol
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("could not fetch %s from Yahoo: %s" % (sym, last))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Correlation check data (trader-risk).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("symbols", nargs="+")
    f.add_argument("--range", default="6mo")
    s = sub.add_parser("show")
    s.add_argument("symbols", nargs="+")
    s.add_argument("--days", type=int, default=DEFAULT_DAYS)
    for p in (f, s):
        p.add_argument("--file", default=CLOSES_PATH)
    a = ap.parse_args(argv)
    syms = [x.strip().upper() for x in a.symbols]
    if a.cmd == "fetch":
        closes = read_closes(a.file) or {}
        for sym in syms:
            try:
                closes[sym] = fetch_daily(sym, a.range)
                print("%s: %d daily closes" % (sym, len(closes[sym])))
            except RuntimeError as e:
                print("correlation.py: %s" % e, file=sys.stderr)
        d = os.path.dirname(a.file)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.file, "w", encoding="utf-8") as fh:
            json.dump({"fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "closes": closes}, fh)
        return 0
    closes = read_closes(a.file) or {}
    print("      " + " ".join("%6s" % x for x in syms))
    for x in syms:
        cells = []
        for y in syms:
            r, _ = corr(closes, x, y, a.days)
            cells.append("%6s" % ("-" if r is None else "%.2f" % r))
        print("%6s" % x + " ".join(cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
