import datetime
import unittest

from _load import load

race = load("trader-race", "race")


def closes(n=260, moves=None):
    """Every symbol rising 0.1%/day; `moves` = {SYM: {day_index: return}} overrides."""
    moves = moves or {}
    d, dates = datetime.date(2025, 9, 1), []
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += datetime.timedelta(days=1)
    out = {}
    for s in race.symbols():
        c, rows = 100.0, []
        for i, dt in enumerate(dates):
            c *= 1 + moves.get(s, {}).get(i, 0.001)
            rows.append([dt, c])
        out[s] = rows
    return out, dates


def bull(by, upto):
    return "bull"


class Race(unittest.TestCase):
    def test_start_charges_entry_and_sets_lines(self):
        cl, dates = closes()
        st = race.start(cl, dates[-5], bull)
        self.assertEqual(st["days"][0]["target_1y"], 200.0)
        self.assertLess(st["bots"]["all_in"]["gbp"], 200.0)       # cost of going in
        self.assertEqual(list(st["bots"]["all_in"]["weights"].values()), [1.0])

    def test_all_in_picks_best_3_day_momentum(self):
        cl, dates = closes(moves={"SOXL": {257: 0.05}})
        st = race.start(cl, dates[-2], bull)
        self.assertEqual(st["bots"]["all_in"]["weights"], {"SOXL": 1.0})

    def test_step_compounds_next_day_move_and_tracks_targets(self):
        cl, dates = closes(moves={"SOXL": {256: 0.05, 259: 0.10}})
        st = race.start(cl, dates[-3], bull)
        g0 = st["bots"]["all_in"]["gbp"]
        st, done = race.step(st, cl, bull)
        self.assertEqual(done, dates[-2:])
        day1 = st["days"][1]["all_in"]
        self.assertAlmostEqual(day1["ret_pct"], 0.1)
        self.assertAlmostEqual(st["days"][2]["all_in"]["ret_pct"], 10.0)
        self.assertGreater(st["bots"]["all_in"]["gbp"], g0 * 1.1 * 0.99)
        self.assertAlmostEqual(st["days"][2]["target_1y"], round(200 * 1.0344 ** 2, 2))

    def test_no_look_ahead(self):
        # SOXL leads up to day 258; TQQQ jumps 50% on day 259, after the decision
        cl, dates = closes(moves={"SOXL": {257: 0.02}, "TQQQ": {259: 0.5}})
        st = race.start(cl, dates[-2], bull)
        self.assertEqual(st["bots"]["all_in"]["weights"], {"SOXL": 1.0})

    def test_floor_at_zero(self):
        cl, dates = closes(moves={"SOXL": {256: 0.05, 259: -1.5}})
        st = race.start(cl, dates[-3], bull)
        st["bots"]["all_in"]["weights"] = {"SOXL": 1.0}
        st, _ = race.step(st, cl, bull)
        self.assertGreaterEqual(st["bots"]["all_in"]["gbp"], 0.0)

    def test_crash_keeps_safe_bot_in_cash(self):
        cl, dates = closes(moves={s: {257: -0.03, 258: -0.03, 259: -0.03} for s in race.SAFE})
        st = race.start(cl, dates[-1], lambda by, upto: "crash")
        self.assertEqual(st["bots"]["safe"]["weights"], {})

    def test_step_is_idempotent(self):
        cl, dates = closes()
        st = race.start(cl, dates[-3], bull)
        st, a = race.step(st, cl, bull)
        st, b = race.step(st, cl, bull)
        self.assertEqual((len(a), b), (2, []))


if __name__ == "__main__":
    unittest.main()
