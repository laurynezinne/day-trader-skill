import json
import os
import tempfile
import unittest

from _load import load, ROOT, FIX

breakers = load("trader-risk", "breakers")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
TSLA_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
NOW = check.parse_now("2026-10-19 10:15")
TODAY = "2026-10-19"   # a Monday


def eq(*rows):
    return {"snapshots": [{"date": d, "equity": e} for d, e in rows]}


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def tsla():
    with open(TSLA_PATH) as fh:
        doc = json.load(fh)
    return doc.get("orders", doc)


class Evaluate(unittest.TestCase):
    def test_no_log_is_ok(self):
        r = breakers.evaluate({"snapshots": []}, TODAY)
        self.assertEqual((r["level"], r["factor"]), ("ok", 1.0))

    def test_small_dip_is_ok(self):
        r = breakers.evaluate(eq(("2026-10-16", 5000), (TODAY, 4950)), TODAY)
        self.assertEqual(r["level"], "ok")
        self.assertEqual(r["numbers"]["day_pct"], -1.0)

    def test_day_down_2pct_halves(self):
        r = breakers.evaluate(eq(("2026-10-16", 5000), (TODAY, 4900)), TODAY)
        self.assertEqual((r["level"], r["factor"]), ("halve", 0.5))
        self.assertIn("day: down 2.00%", r["reasons"][0])

    def test_day_down_3pct_stops_and_flattens(self):
        r = breakers.evaluate(eq(("2026-10-16", 5000), (TODAY, 4850)), TODAY)
        self.assertEqual(r["level"], "stop")
        self.assertTrue(r["flatten"])
        self.assertFalse(r["trip_halt"])

    def test_week_down_5pct_halves(self):
        # Friday before this week 5000; slow bleed, never 2% in one day
        r = breakers.evaluate(eq(("2026-10-16", 5000), ("2026-10-19", 4900.5), ("2026-10-20", 4820),
                                 ("2026-10-21", 4750)), "2026-10-21")
        self.assertEqual(r["level"], "halve")
        self.assertIn("week: down 5.00%", r["reasons"][0])

    def test_peak_10pct_halts(self):
        r = breakers.evaluate(eq(("2026-10-01", 5500), ("2026-10-16", 5000), (TODAY, 4950)), TODAY)
        self.assertEqual(r["level"], "halt")
        self.assertTrue(r["trip_halt"])

    def test_lock_file_halts_even_when_recovered(self):
        r = breakers.evaluate(eq(("2026-10-16", 5000), (TODAY, 5100)), TODAY, halted="peak halt\n")
        self.assertEqual(r["level"], "halt")
        self.assertFalse(r["trip_halt"])
        self.assertIn("delete trader/state/HALTED", r["reasons"][0])

    def test_rules_can_be_switched_off(self):
        r = breakers.evaluate(eq(("2026-10-16", 5000), (TODAY, 4850)), TODAY,
                              {"breaker_day_stop_pct": 0, "breaker_day_halve_pct": None})
        self.assertEqual(r["level"], "ok")

    def test_future_rows_ignored(self):
        r = breakers.evaluate(eq(("2026-10-16", 5000), (TODAY, 5000), ("2026-10-20", 4000)), TODAY)
        self.assertEqual(r["level"], "ok")


class InCheck(unittest.TestCase):
    def run_check(self, brk, **over):
        return check.check(tsla(), cfg(**over), None, {"TSLA": 364.18}, None, TODAY, NOW, None, brk)

    def test_baseline_passes(self):
        res = self.run_check(None)
        self.assertEqual(res["block"], 0)
        self.assertEqual(res["breakers"]["level"], "ok")

    def test_stop_blocks_entries(self):
        brk = breakers.evaluate(eq(("2026-10-16", 5000), (TODAY, 4850)), TODAY)
        res = self.run_check(brk)
        self.assertEqual(res["pass"], 0)
        self.assertTrue(any(x.startswith("breaker: day stop") for x in res["results"][0]["reasons"]))
        self.assertTrue(any("flatten" in w for w in res["warnings"]))

    def test_halve_cuts_max_loss(self):
        brk = breakers.evaluate(eq(("2026-10-16", 5000), (TODAY, 4900)), TODAY)
        res = self.run_check(brk)   # the TSLA bracket risks $46.88, over the halved $25
        self.assertEqual(res["block"], 1)
        self.assertTrue(any("over your $25 max loss" in x for x in res["results"][0]["reasons"]))

    def test_auto_refuses_live(self):
        with self.assertRaises(check.RiskError):
            self.run_check(None, auto=True, paper=False)

    def test_auto_on_paper_runs(self):
        res = self.run_check(None, auto=True, paper=True)
        self.assertTrue(res["auto"])

    def test_main_writes_lock_file_once(self):
        with tempfile.TemporaryDirectory() as d:
            ep, hp, out = (os.path.join(d, x) for x in ("equity.json", "HALTED", "risk.json"))
            with open(ep, "w") as fh:
                json.dump(eq(("2026-10-01", 5600), (TODAY, 5000)), fh)
            cp = os.path.join(d, "config.json")
            with open(cp, "w") as fh:
                json.dump(cfg(), fh)
            args = ["--orders", TSLA_PATH, "--config", cp, "--now", "2026-10-19 10:15", "--equity", ep,
                    "--halt-file", hp, "--out", out, "--portfolio", os.path.join(d, "none.json"),
                    "--prices", os.path.join(d, "none.json"), "--state", os.path.join(d, "none.json")]
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(args), 1)
            self.assertTrue(os.path.exists(hp))
            with open(hp) as fh:
                first = fh.read()
            with redirect_stdout(io.StringIO()):
                check.main(args)
            with open(hp) as fh:
                self.assertEqual(fh.read(), first)


if __name__ == "__main__":
    unittest.main()
