import datetime
import json
import math
import os
import unittest

from _load import load, ROOT, FIX

corr = load("trader-risk", "correlation")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
TSLA_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
NOW = check.parse_now("2026-10-19 10:15")
TODAY = "2026-10-19"


def series(rets, start=100.0, skip=()):
    """Daily closes from a list of returns, business days from 2026-07-01."""
    d = datetime.date(2026, 7, 1)
    out, c = [], start
    for i, r in enumerate(rets):
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        c *= 1 + r
        if i not in skip:
            out.append([d.isoformat(), round(c, 4)])
        d += datetime.timedelta(days=1)
    return out


WAVE = [0.01 * math.sin(i * 0.7) + 0.004 * math.cos(i * 1.9) for i in range(80)]
OTHER = [0.01 * math.sin(i * 1.3 + 2) - 0.006 * math.cos(i * 0.4) for i in range(80)]


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def tsla():
    with open(TSLA_PATH) as fh:
        doc = json.load(fh)
    return doc.get("orders", doc)


class Maths(unittest.TestCase):
    def test_identical_moves_are_1(self):
        r, n = corr.corr({"A": series(WAVE), "B": series(WAVE, 50)}, "A", "B")
        self.assertAlmostEqual(r, 1.0, places=6)
        self.assertEqual(n, 60)

    def test_opposite_moves_are_minus_1(self):
        r, _ = corr.corr({"A": series(WAVE), "B": series([-x for x in WAVE])}, "A", "B")
        self.assertLess(r, -0.99)

    def test_matched_by_date_not_position(self):
        # B is missing day 10; matching by date keeps the rest aligned
        r, n = corr.corr({"A": series(WAVE), "B": series(WAVE, skip={10})}, "A", "B")
        self.assertGreater(r, 0.95)

    def test_too_little_history_gives_no_verdict(self):
        r, n = corr.corr({"A": series(WAVE[:10]), "B": series(WAVE[:10])}, "A", "B")
        self.assertIsNone(r)
        self.assertEqual(n, 9)

    def test_conflicts_and_switch_off(self):
        closes = {"A": series(WAVE), "B": series(WAVE), "C": series(OTHER)}
        blocks, warns = corr.conflicts("A", ["B", "C"], closes)
        self.assertEqual([b[0] for b in blocks], ["B"])
        self.assertEqual(warns, [])
        self.assertEqual(corr.conflicts("A", ["B"], closes, {"max_correlation": 0}), ([], []))


class InCheck(unittest.TestCase):
    PF = {"cash": 4000.0, "positions": [{"ticker": "NIO", "shares": 10, "price": 5.0}]}

    def run_check(self, closes):
        return check.check(tsla(), cfg(), self.PF, {"TSLA": 364.18, "NIO": 5.0}, None, TODAY, NOW, None,
                           None, closes)

    def test_correlated_with_a_holding_blocks(self):
        res = self.run_check({"TSLA": series(WAVE), "NIO": series(WAVE, 5)})
        self.assertEqual(res["pass"], 0)
        self.assertTrue(any(x.startswith("correlation: TSLA and NIO") for x in res["results"][0]["reasons"]))

    def test_unrelated_holding_passes(self):
        res = self.run_check({"TSLA": series(WAVE), "NIO": series(OTHER, 5)})
        self.assertEqual(res["block"], 0)

    def test_missing_history_warns_but_passes(self):
        res = self.run_check({"TSLA": series(WAVE)})
        self.assertEqual(res["block"], 0)
        self.assertTrue(any(w.startswith("correlation: not enough") for w in res["warnings"]))

    def test_no_closes_file_skips(self):
        res = self.run_check(None)
        self.assertEqual(res["block"], 0)


if __name__ == "__main__":
    unittest.main()
