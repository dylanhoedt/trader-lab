import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

paper = load("trader-broker", "paper")
ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-reel.json")
QUOTES = os.path.join(FIX, "trader-alerts")
TODAY = "2026-10-13"
OPEN_UTC = "2026-10-13T15:00"     # 11:00 New York, a Tuesday
CLOSED_UTC = "2026-10-13T06:00"   # 02:00 New York, before the open


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class FillTerms(unittest.TestCase):
    def test_market_notional_slips_against_you(self):
        ok, qty, px, _ = paper.fill_terms({"side": "buy", "notional": "1000", "type": "market"}, 100.0, 10)
        self.assertTrue(ok)
        self.assertEqual(px, 100.1)
        self.assertAlmostEqual(qty * px, 1000, places=2)
        ok, qty, px, _ = paper.fill_terms({"side": "sell", "qty": "2", "type": "market"}, 100.0, 10)
        self.assertEqual((qty, px), (2.0, 99.9))

    def test_limit_fills_only_when_marketable(self):
        o = {"side": "buy", "qty": "1", "type": "limit", "limit_price": "99"}
        self.assertFalse(paper.fill_terms(o, 100.0, 0)[0])
        self.assertEqual(paper.fill_terms(o, 98.0, 0)[2], 98.0)


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        j = lambda *p: os.path.join(self.d, *p)
        self.orders, self.cfg, self.pf = j("orders.json"), j("config.json"), j("portfolio.json")
        self.risk, self.state, self.book = j("state", "risk.json"), j("state", "broker.json"), j("state", "paper.json")
        shutil.copy(ORDERS_PATH, self.orders)
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        cfg.update(broker="paper", paper_slippage_bps=0)
        with open(self.cfg, "w") as fh:
            json.dump(cfg, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def common(self):
        return ["--config", self.cfg, "--portfolio", self.pf, "--book", self.book, "--offline", QUOTES]

    def execute(self, now, index="1"):
        return run(paper, ["execute", "--index", index, "--orders", self.orders, "--state", self.state,
                           "--today", TODAY, "--now", now] + self.common())

    def approve(self, reply="yes"):
        with redirect_stdout(io.StringIO()):
            check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                        "--prices", os.path.join(self.d, "none"), "--state", self.state,
                        "--today", TODAY, "--out", self.risk])
        return run(ticket, ["approve", "--index", "1", "--reply", reply, "--orders", self.orders,
                            "--config", self.cfg, "--risk", self.risk, "--state", self.state, "--today", TODAY])

    def test_no_yes_no_fill(self):
        self.assertEqual(run(paper, ["reset", "--confirm"] + self.common())[0], 0)
        code, _, err = self.execute(OPEN_UTC)
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        self.assertFalse(os.path.exists(self.book) and json.load(open(self.book))["orders"])

    def test_fill_while_open_then_one_yes_is_spent(self):
        run(paper, ["reset", "--confirm"] + self.common())
        self.assertEqual(self.approve()[0], 0)
        code, text, _ = self.execute(OPEN_UTC)
        self.assertEqual(code, 0, text)
        self.assertIn("Bought", text)
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 3750.0)
        self.assertEqual(pf["positions"][0]["lots"][0]["cost"], 1250.0)
        self.assertAlmostEqual(pf["positions"][0]["shares"], 1250 / 341.27, places=5)
        self.assertEqual(self.execute(OPEN_UTC)[0], 1)  # the same yes does not fill twice
        with open(self.state) as fh:
            self.assertEqual([r["status"] for r in json.load(fh)["orders"]], ["approved", "placed"])

    def test_queued_while_closed_then_settled(self):
        run(paper, ["reset", "--confirm"] + self.common())
        self.approve()
        code, text, _ = self.execute(CLOSED_UTC)
        self.assertIn("Queued", text)
        self.assertEqual(json.load(open(self.pf))["cash"], 5000.0)
        self.assertEqual(run(paper, ["settle", "--now", CLOSED_UTC] + self.common())[0], 1)
        code, text, _ = run(paper, ["settle", "--now", OPEN_UTC] + self.common())
        self.assertIn("Bought", text)
        self.assertEqual(json.load(open(self.book))["orders"][0]["status"], "filled")

    def test_needs_the_paper_broker_in_the_config(self):
        cfg = json.load(open(self.cfg))
        cfg["broker"] = "alpaca"
        json.dump(cfg, open(self.cfg, "w"))
        self.approve()
        code, _, err = self.execute(OPEN_UTC)
        self.assertEqual(code, 1)
        self.assertIn('"broker": "paper"', err)

    def test_reset_needs_confirm(self):
        self.assertEqual(run(paper, ["reset"] + self.common())[0], 1)
        self.assertFalse(os.path.exists(self.pf))


if __name__ == "__main__":
    unittest.main()
