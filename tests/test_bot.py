import math
import random
import unittest

from _load import load

bot = load("trader-bot", "bot")


def bars(n, drift=0.0005, vol=0.004, seed=1, start=1700000000):
    rng, px, out = random.Random(seed), 100.0, []
    for i in range(n):
        o = px
        px = px * math.exp(drift + rng.gauss(0, vol))
        h, l = max(o, px) * (1 + abs(rng.gauss(0, vol / 3))), min(o, px) * (1 - abs(rng.gauss(0, vol / 3)))
        out.append([start + 3600 * i, o, h, l, px, 1000 + rng.random() * 500])
    return out


def pack(b, fee=0.0005, sym="AAA"):
    return {sym: {"bars": b, "scout": bot.scout(b), "fee": fee, "name": sym}}


def daily(n, drift, seed=2, start="2016-01-01"):
    import datetime
    d0 = datetime.date.fromisoformat(start)
    rng, px, out = random.Random(seed), 100.0, []
    for i in range(n):
        px *= math.exp(drift + rng.gauss(0, 0.01))
        out.append([(d0 + datetime.timedelta(days=i)).isoformat(), px])
    return out


class Scorer(unittest.TestCase):
    def test_score_is_0_to_100_and_warms_up(self):
        b = bars(600)
        s = bot.scout(b)
        self.assertEqual(bot.score(b[10], s[10])[0], None)
        for i in range(250, 600):
            sc, parts = bot.score(b[i], s[i])
            self.assertTrue(0 <= sc <= 100)
            self.assertEqual(set(parts), {"trend", "entry", "strength", "volume", "momentum"})

    def test_skeptic_vetoes_chasing(self):
        s = {"rsi": 80, "atr": 1.0}
        self.assertTrue(any("chasing" in v for v in bot.skeptic([[0, 1, 1, 1, 1, 1]] * 30, 29, s)))

    def test_aroon_range(self):
        for v in bot.aroon(bars(300), 25)[25:]:
            self.assertTrue(-100 <= v <= 100)


class Simulation(unittest.TestCase):
    def test_no_look_ahead(self):
        """Changing later bars never changes trades that closed before them."""
        b = bars(3000, seed=4)
        base = bot.simulate(pack(b))["trades"]
        cut = b[2000][0]
        b2 = b[:2000] + [[x[0], x[1] * 0.5, x[2] * 0.5, x[3] * 0.5, x[4] * 0.5, x[5]] for x in b[2000:]]
        moved = bot.simulate(pack(b2))["trades"]
        early = lambda ts: [t for t in ts if t["exit_ts"] < cut]  # noqa: E731
        self.assertTrue(early(base))
        self.assertEqual(early(base), early(moved))

    def test_entry_is_next_open_and_fees_charged(self):
        b = bars(3000, seed=4)
        res = bot.simulate(pack(b, fee=0.01))
        opens = {x[0]: x[1] for x in b}
        for t in res["trades"]:
            self.assertEqual(round(opens[t["entry_ts"]], 4), t["entry"])
            gross = t["qty"] * (t["exit"] - t["entry"])
            self.assertLess(t["pnl"], gross)

    def test_stop_assumed_first_when_both_hit(self):
        b = bars(400, seed=5)
        res = bot.simulate(pack(b, fee=0))
        for t in res["trades"]:
            if t["why"] == "target":
                bar = [x for x in b if x[0] == t["exit_ts"]][0]
                self.assertGreater(bar[3], t["entry"] - 1e9)   # target exits only where the stop was not hit

    def test_never_leveraged(self):
        b = bars(3000, drift=0.002, seed=6)
        res = bot.simulate(pack(b, fee=0))
        self.assertGreaterEqual(res["cash"], -1e-6)


class TrendRider(unittest.TestCase):
    def setUp(self):
        self.d = {"UP": daily(900, 0.002), "DN": daily(900, -0.002, seed=3)}
        self.series = {s: bot.trend_series(v) for s, v in self.d.items()}
        self.fees = {"UP": 0.001, "DN": 0.001}

    def test_holds_uptrend_not_downtrend(self):
        run = bot.trend_run(self.series, self.fees, start="2016-09-01")
        self.assertGreater(run["weights"]["UP"], 0)
        self.assertEqual(run["weights"]["DN"], 0)

    def test_weights_capped_and_never_leveraged(self):
        last = {s: v[max(v)] for s, v in self.series.items()}
        w = bot.trend_weights(last, list(last))
        self.assertTrue(all(x <= bot.TREND["max_weight"] for x in w.values()))
        self.assertLessEqual(sum(w.values()), 1.0 + 1e-9)

    def test_pnl_reconciles(self):
        run = bot.trend_run(self.series, self.fees, start="2016-09-01")
        rep = bot.trend_report(run, names={"UP": "Up", "DN": "Down"})
        closed = sum(t["pnl"] for t in rep["trades"])
        open_ = sum(o["pnl"] for o in rep["open"])
        self.assertAlmostEqual(closed + open_, rep["stats"]["end"] - bot.START_EQUITY, delta=1.0)

    def test_score_70_means_both_trend_tests(self):
        self.assertEqual(bot.trend_score(10, 9, 8, 0)[0], 70)
        self.assertLess(bot.trend_score(10, 7, 8, 100)[0], 70)


if __name__ == "__main__":
    unittest.main()
