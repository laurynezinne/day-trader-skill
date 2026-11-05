import datetime
import math
import random
import unittest

from _load import load

v = load("trader-validate", "validate")


def bdays(n, start=datetime.date(2012, 1, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def walk(n, drift=0.0, vol=0.01, seed=1, revert=0.0):
    """Log-price random walk; revert > 0 pulls price back toward its trend (mean reversion)."""
    rng = random.Random(seed)
    lp, trend, out = 0.0, 0.0, []
    for _ in range(n):
        trend += drift
        lp += drift + rng.gauss(0, vol) - revert * (lp - trend)
        out.append(100 * math.exp(lp))
    return out


class Indicators(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(v.sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])

    def test_rsi_extremes(self):
        up = v.rsi([float(i) for i in range(1, 20)], 2)
        self.assertEqual(up[-1], 100.0)
        down = v.rsi([float(20 - i) for i in range(20)], 2)
        self.assertAlmostEqual(down[-1], 0.0)


class NoLookAhead(unittest.TestCase):
    def test_todays_position_earns_tomorrow(self):
        closes = [100, 110, 121, 133.1]
        r, _ = v.daily_returns(closes, [0, 0, 0, 1], 0, 4)
        self.assertEqual(r, [0.0, 0.0, 0.0])            # bought at the last close: no return yet
        r2, _ = v.daily_returns(closes, [1, 1, 1, 1], 0, 4)
        self.assertAlmostEqual(r2[0], 0.1 - v.COST)     # entry cost once, then the day's move

    def test_test_year_never_moves_the_chosen_settings(self):
        n = 252 * 5
        dates, c = bdays(n), walk(n, 0.0003, seed=3, revert=0.05)
        base = v.walk_forward(dates, c, "rsi_reversion")["chosen"][0]
        yrs = v.year_index(dates)
        te_s, te_e = yrs[3][1], yrs[3][2]
        c2 = c[:te_s] + [x * (1.5 if i % 2 else 0.6) for i, x in enumerate(c[te_s:te_e])] + c[te_e:]
        moved = v.walk_forward(dates, c2, "rsi_reversion")["chosen"][0]
        self.assertEqual(base["settings"], moved["settings"])
        self.assertEqual(base["train_sharpe"], moved["train_sharpe"])


class Gauntlet(unittest.TestCase):
    def test_costs_are_charged(self):
        closes = [100.0] * 10
        r, t = v.daily_returns(closes, [0, 1, 0, 1, 0, 1, 0, 1, 0, 0], 0, 10)
        self.assertLess(sum(r), 0)
        self.assertTrue(all(x < 0 for x in t))

    def test_drawdown_and_sharpe(self):
        s = v.stats([0.01, -0.5, 0.01])
        self.assertGreaterEqual(s["max_dd_pct"], 50.0)
        self.assertEqual(v.stats([0.001] * 10)["sharpe"], 0.0)   # no variance -> no Sharpe claimed

    def test_pure_noise_fails(self):
        n = 252 * 8
        res = v.gauntlet(bdays(n), walk(n, 0.0, 0.012, seed=11), "rsi_reversion", "NOISE")
        self.assertEqual(res["verdict"], "FAIL")
        self.assertIn("sharpe", res["failed"])

    def test_record_shape(self):
        n = 252 * 6
        res = v.gauntlet(bdays(n), walk(n, 0.0004, 0.01, seed=5, revert=0.08), "sma_trend", "SYN")
        for k in ("out_of_sample", "baselines", "checks", "verdict", "settings_by_year", "current_settings"):
            self.assertIn(k, res)
        self.assertEqual(set(res["checks"]), {"sharpe", "drawdown", "trades", "not_overfit", "bootstrap"})
        self.assertIn("random_same_exposure_sharpe", res["baselines"])

    def test_too_short_refuses(self):
        with self.assertRaises(v.ValidateError):
            v.gauntlet(bdays(400), walk(400), "rsi_reversion", "SHORT")


if __name__ == "__main__":
    unittest.main()
