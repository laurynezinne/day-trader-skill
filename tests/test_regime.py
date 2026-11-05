import datetime
import json
import os
import random
import unittest

from _load import load, ROOT, FIX

regime = load("trader-regime", "regime")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
TSLA_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
NOW = check.parse_now("2026-10-19 10:15")
TODAY = "2026-10-19"


def closes_from(rets, start=100.0):
    d = datetime.date(2020, 1, 1)
    out, c = [], start
    for r in rets:
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        c *= 1 + r
        out.append([d.isoformat(), round(c, 6)])
        d += datetime.timedelta(days=1)
    return out


def two_regimes(seed=7, blocks=6, length=60):
    """Calm rising blocks alternating with volatile falling blocks."""
    rng = random.Random(seed)
    rets, truth = [], []
    for b in range(blocks):
        calm = b % 2 == 0
        for _ in range(length):
            rets.append(rng.gauss(0.0012, 0.005) if calm else rng.gauss(-0.003, 0.025))
            truth.append("calm" if calm else "storm")
    return rets, truth


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def tsla():
    with open(TSLA_PATH) as fh:
        doc = json.load(fh)
    return doc.get("orders", doc)


class Model(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rets, cls.truth = two_regimes()
        cls.dates, cls.X = regime.features(closes_from(rets))
        cls.model = regime.fit(cls.X, 2, seed=0)

    def test_features_shape(self):
        self.assertEqual(len(self.X), len(self.dates))
        self.assertEqual(len(self.X[0]), 2)
        self.assertGreater(min(x[1] for x in self.X), 0)

    def test_states_sorted_by_return(self):
        means = [m[0] for m in self.model["means"]]
        self.assertEqual(means, sorted(means))
        self.assertEqual(self.model["names"], ["bear", "bull"])

    def test_finds_the_planted_regimes(self):
        alphas, _ = regime.forward(self.X, self.model)
        # features start VOL_DAYS days into the returns; align the truth
        truth = self.truth[regime.VOL_DAYS:]
        hits = sum(1 for a, t in zip(alphas, truth)
                   if (a[1] > a[0]) == (t == "calm"))
        self.assertGreater(hits / len(truth), 0.85)

    def test_forward_pass_has_no_look_ahead(self):
        cut = 200
        a_short, _ = regime.forward(self.X[:cut], self.model)
        a_long, _ = regime.forward(self.X, self.model)
        for t in range(cut):
            for k in range(2):
                self.assertAlmostEqual(a_short[t][k], a_long[t][k], places=12)

    def test_falling_type_is_the_volatile_one(self):
        # the bear state should carry the storm's higher volatility
        self.assertGreater(self.model["means"][0][1], self.model["means"][1][1])

    def test_bic_charges_for_extra_types(self):
        m = dict(self.model, loglik=1000.0)
        m3 = dict(regime.fit(self.X, 3, seed=0), loglik=1000.0)
        self.assertLess(regime.bic(m, len(self.X)), regime.bic(m3, len(self.X)))

    def test_too_little_data_refuses(self):
        with self.assertRaises(regime.RegimeError):
            regime.fit(self.X[:30], 3)


class Stability(unittest.TestCase):
    def test_one_day_flip_is_ignored(self):
        a = [[0.9, 0.1]] * 5 + [[0.1, 0.9]] + [[0.9, 0.1]] * 3
        self.assertEqual(regime.stable_path(a), [0] * 9)

    def test_three_days_confirm_a_change(self):
        a = [[0.9, 0.1]] * 3 + [[0.1, 0.9]] * 4
        self.assertEqual(regime.stable_path(a), [0, 0, 0, 0, 0, 1, 1])

    def test_classify_reports_pending_change(self):
        model = {"n": 2, "names": ["bear", "bull"], "means": [[-0.01, 0.03], [0.002, 0.006]],
                 "vars": [[0.0004, 0.0001], [0.00002, 0.00001]], "trans": [[0.95, 0.05], [0.05, 0.95]],
                 "start": [0.5, 0.5]}
        X = [[0.002, 0.006]] * 20 + [[-0.05, 0.03]]
        res = regime.classify(["d%d" % i for i in range(len(X))], X, model)
        self.assertEqual(res["regime"], "bull")
        self.assertEqual(res["most_likely_today"], "bear")
        self.assertTrue(res["pending_change"])
        self.assertEqual(res["factor"], 1.0)


class InCheck(unittest.TestCase):
    def run_check(self, reg):
        return check.check(tsla(), cfg(), None, {"TSLA": 364.18}, None, TODAY, NOW, None, None, None, reg)

    def test_crash_blocks_buys(self):
        res = self.run_check({"regime": "crash", "factor": 0.0, "date": TODAY})
        self.assertEqual(res["pass"], 0)
        self.assertTrue(any(x.startswith("regime: the market type is crash")
                            for x in res["results"][0]["reasons"]))

    def test_bear_halves_max_loss(self):
        res = self.run_check({"regime": "bear", "factor": 0.5, "date": TODAY})
        self.assertEqual(res["block"], 1)   # $46.88 at risk vs a $25 cap
        self.assertTrue(any("over your $25 max loss" in x for x in res["results"][0]["reasons"]))
        self.assertTrue(any(w.startswith("regime: the market type is bear") for w in res["warnings"]))

    def test_bull_changes_nothing(self):
        res = self.run_check({"regime": "bull", "factor": 1.0, "date": TODAY})
        self.assertEqual(res["block"], 0)
        self.assertEqual(res["regime"]["regime"], "bull")

    def test_no_regime_changes_nothing(self):
        self.assertEqual(self.run_check(None)["block"], 0)


if __name__ == "__main__":
    unittest.main()
