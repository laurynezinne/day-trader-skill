import datetime
import json
import os
import tempfile
import unittest

from _load import load, ROOT

auto = load("trader-auto", "auto")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
NOW = check.parse_now("2026-10-19 15:20")      # New York; a Monday, inside the entry window
TODAY = "2026-10-19"


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update({"auto": True, "paper": True})
    c.update(over)
    return c


def hist(closes, end=datetime.date(2026, 10, 16)):
    out, d = [], end
    for c in reversed(closes):
        while d.weekday() >= 5:
            d -= datetime.timedelta(days=1)
        out.append([d.isoformat(), c])
        d -= datetime.timedelta(days=1)
    return list(reversed(out))


# 260 days rising (above the 200-day), then a sharp 3-day dip: RSI(2) oversold -> buy signal
UP = [100 * (1.001 ** i) for i in range(260)]
DIP = UP + [UP[-1] * 0.97, UP[-1] * 0.94, UP[-1] * 0.92]


class FakeBroker:
    def __init__(self, is_open=True, equity=5000.0, positions=None):
        self.is_open, self.equity, self.pos = is_open, equity, positions or []
        self.sent, self.closed, self.closed_all, self.cancelled = [], [], False, []

    def clock(self):
        return {"is_open": self.is_open}

    def account(self):
        return {"equity": str(self.equity), "cash": str(self.equity)}

    def positions(self):
        return self.pos

    def open_orders(self, sym):
        return [{"id": "leg-" + sym}]

    def cancel(self, oid):
        self.cancelled.append(oid)

    def close_position(self, sym):
        self.closed.append(sym)
        return {"id": "close-" + sym}

    def close_all(self):
        self.closed_all = True

    def submit(self, order):
        self.sent.append(order)
        return {"id": "o%d" % len(self.sent)}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.paths = {k: os.path.join(d, v) for k, v in {
            "equity": "equity.json", "halt": "HALTED", "regime": "regime.json", "validation": "val.json",
            "state": "broker.json", "journal": "journal"}.items()}
        self.write("validation", {"strategy": "rsi_reversion", "passed": ["AAA"], "results": [
            {"symbol": "AAA", "verdict": "PASS", "current_settings": {"rsi_buy": 10, "exit_sma": 5}}]})

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, key, doc):
        with open(self.paths[key], "w") as fh:
            json.dump(doc, fh)

    def go(self, broker, closes=DIP, dry=False, **over):
        return auto.run(cfg(**over), broker, lambda s: hist(closes), now=NOW, dry_run=dry, paths=self.paths)


class Guards(Base):
    def test_refuses_when_auto_off(self):
        with self.assertRaises(auto.AutoError):
            auto.run(cfg(auto=False), FakeBroker(), lambda s: [], now=NOW, paths=self.paths)

    def test_refuses_live(self):
        with self.assertRaises(auto.AutoError):
            auto.run(cfg(paper=False), FakeBroker(), lambda s: [], now=NOW, paths=self.paths)

    def test_paper_url_is_fixed(self):
        self.assertEqual(auto.PAPER_URL, "https://paper-api.alpaca.markets")

    def test_no_keys_refuses(self):
        old = {k: os.environ.pop(k, None) for k in
               ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY")}
        try:
            with self.assertRaises(auto.AutoError):
                auto.AlpacaPaper()
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v

    def test_market_closed_does_nothing(self):
        b = FakeBroker(is_open=False)
        rep = self.go(b)
        self.assertEqual(rep["skipped"], "market closed")
        self.assertEqual(b.sent, [])


class Trading(Base):
    def test_signal_sends_a_bracket_after_risk_passes(self):
        b = FakeBroker()
        rep = self.go(b)
        self.assertEqual(rep["signals"], {"AAA": 1})
        self.assertEqual(len(b.sent), 1)
        o = b.sent[0]
        self.assertEqual((o["symbol"], o["side"], o["order_class"]), ("AAA", "buy", "bracket"))
        loss = (DIP[-1] - float(o["stop_loss"]["stop_price"])) * int(o["qty"])
        self.assertLessEqual(loss, 50 + 1e-6)
        with open(self.paths["state"]) as fh:
            rec = json.load(fh)["orders"][0]
        self.assertEqual((rec["kind"], rec["status"], rec["auto"]), ("entry", "placed", True))
        self.assertTrue(os.listdir(self.paths["journal"]))

    def test_dry_run_sends_nothing(self):
        b = FakeBroker()
        rep = self.go(b, dry=True)
        self.assertEqual(b.sent, [])
        self.assertTrue(any(l.startswith("[dry run] would ENTER") for l in rep["lines"]))

    def test_crash_market_type_blocks_entry(self):
        self.write("regime", {"regime": "crash", "factor": 0.0, "date": TODAY})
        b = FakeBroker()
        rep = self.go(b)
        self.assertEqual(b.sent, [])
        self.assertTrue(any(l.startswith("BLOCKED AAA") and "regime" in l for l in rep["lines"]))

    def test_bear_market_sizes_to_the_reduced_max_loss(self):
        # found in the first real dry run: sizing to $50 while trader-risk allows $25 blocked every entry
        self.write("regime", {"regime": "bear", "factor": 0.5, "date": TODAY})
        b = FakeBroker()
        rep = self.go(b)
        self.assertEqual(len(b.sent), 1, rep["lines"])
        o = b.sent[0]
        loss = (DIP[-1] - float(o["stop_loss"]["stop_price"])) * int(o["qty"])
        self.assertLessEqual(loss, 25 + 1e-6)
        self.assertTrue(any("max loss of $25.00" in l for l in rep["lines"]))

    def test_exit_when_rule_no_longer_holds(self):
        b = FakeBroker(positions=[{"symbol": "AAA", "qty": "5", "current_price": str(UP[-1] * 1.02)}])
        rep = self.go(b, closes=UP)
        self.assertEqual(b.closed, ["AAA"])
        self.assertEqual(b.cancelled, ["leg-AAA"])
        self.assertIn("AAA", rep["sent"])

    def test_day_stop_flattens_and_stops(self):
        self.write("equity", {"snapshots": [{"date": "2026-10-16", "equity": 5000}]})
        b = FakeBroker(equity=4800)            # -4% today
        rep = self.go(b)
        self.assertTrue(b.closed_all)
        self.assertEqual(b.sent, [])
        self.assertTrue(rep["flattened"])

    def test_peak_halt_writes_lock_file(self):
        self.write("equity", {"snapshots": [{"date": "2026-10-01", "equity": 6000},
                                            {"date": "2026-10-16", "equity": 5450}]})
        b = FakeBroker(equity=5390)            # -10.2% from peak, -1.1% today
        self.go(b)
        self.assertTrue(os.path.exists(self.paths["halt"]))
        self.assertTrue(b.closed_all)

    def test_nothing_validated_trades_nothing(self):
        self.write("validation", {"strategy": "rsi_reversion", "passed": [], "results": []})
        b = FakeBroker()
        rep = self.go(b)
        self.assertEqual(rep["skipped"], "nothing validated")
        self.assertEqual(b.sent, [])


if __name__ == "__main__":
    unittest.main()
