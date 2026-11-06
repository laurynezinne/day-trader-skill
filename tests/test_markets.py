import unittest

from _load import load
from test_regime import closes_from, two_regimes

markets = load("trader-regime", "markets")


class Markets(unittest.TestCase):
    def test_build_keeps_closes_and_a_market_type(self):
        rets, _ = two_regimes()
        hist = closes_from(rets)
        calls = []

        def fetch(sym):
            calls.append(sym)
            if sym == "NG=F":
                raise RuntimeError("rate limited")
            return hist

        doc = markets.build(fetch)
        self.assertEqual(len(calls), len(markets.ASSETS))
        self.assertEqual(len(doc["rows"]), len(markets.ASSETS) - 1)      # one bad symbol never stops the rest
        self.assertEqual(doc["errors"], ["NG=F: rate limited"])
        r = doc["rows"][0]
        self.assertEqual(len(r["closes"]), markets.KEEP_DAYS)
        self.assertIn(r["regime"], ("crash", "bear", "neutral", "bull", "euphoria"))
        self.assertEqual(r["asof"], hist[-1][0])
        self.assertNotIn("d1", r)       # price changes are the dashboard's job, so readers can check them


if __name__ == "__main__":
    unittest.main()
