import json
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger, CallUncertain, guarded_call


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.jsonl"
        self.ledger = BudgetLedger(self.path)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "evals.brainstorm", "ledger", "--ledger",
                               str(self.path), *args], capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL)

    def unknown(self, key="unknown"):
        with self.assertRaises(CallUncertain):
            guarded_call(self.ledger, FakeBackend(failure="unknown", reserve_usd="0.2"),
                         key, "p", "m", 1)

    def test_help_and_status_are_registered(self):
        help_result = subprocess.run([sys.executable, "-m", "evals.brainstorm", "--help"],
                                     capture_output=True, text=True, timeout=10)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("ledger", help_result.stdout)
        self.unknown()
        guarded_call(self.ledger, FakeBackend(failure="missing_cost", reserve_usd="0.3"),
                     "estimated", "p", "m", 1)
        guarded_call(self.ledger, FakeBackend(usd="0.1", reserve_usd="0.1"), "paid", "p", "m", 1)
        self.ledger.reserve("live", "0.4")
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual({k: status[k] for k in ("used", "settled", "reserved", "uncertain", "estimated", "remaining")},
                         {"used": 1, "settled": 0.4, "reserved": 0.4, "uncertain": 0.2, "estimated": 0.3, "remaining": 49})

    def test_release_requires_confirmation_and_is_idempotent(self):
        self.unknown()
        before = self.path.read_bytes()
        refused = self.run_cli("release", "unknown")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--confirm-not-charged", refused.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        released = self.run_cli("release", "unknown", "--confirm-not-charged")
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertEqual(json.loads(released.stdout)["used"], 0)
        before = self.path.read_bytes()
        self.assertEqual(self.run_cli("release", "unknown", "--confirm-not-charged").returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_manual_settle_invalid_values_and_idempotence(self):
        self.unknown()
        before = self.path.read_bytes()
        for value in ("-1", "NaN", "Infinity", "bad"):
            result = self.run_cli("settle", "unknown", "--usd", value)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.path.read_bytes(), before)
        result = self.run_cli("settle", "unknown", "--usd", "0.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["used"], 0.1)
        before = self.path.read_bytes()
        self.assertEqual(self.run_cli("settle", "unknown", "--usd", "0.1").returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotEqual(self.run_cli("settle", "unknown", "--usd", "0.2").returncode, 0)
        self.assertNotEqual(self.run_cli("release", "unknown", "--confirm-not-charged").returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cannot_reconcile_live_call(self):
        self.ledger.reserve("live", "0.1")
        before = self.path.read_bytes()
        for args in (("settle", "live", "--usd", "0"), ("release", "live", "--confirm-not-charged")):
            result = self.run_cli(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("live", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_overrun_cli_exit_three_and_delta(self):
        self.unknown()
        result = self.run_cli("settle", "unknown", "--usd", "0.3")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("reserved=0.2 actual=0.3 delta=0.1", result.stderr)
        self.assertEqual(self.ledger.status()["settled"], Decimal("0.3"))

    def test_guarded_call_uses_same_exit_code_boundary(self):
        source = """import sys
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger, guarded_call, cli_boundary
def run(args):
    guarded_call(BudgetLedger(sys.argv[1]), FakeBackend(usd='0.3', reserve_usd='0.2'), 'over', 'p', 'm', 1)
sys.exit(cli_boundary(run, None))
"""
        result = subprocess.run([sys.executable, "-c", source, str(self.path)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 3)
        self.assertIn("delta=0.1", result.stderr)

    def test_cache_failure_overrun_still_exits_three(self):
        source = """import sys
from unittest.mock import patch
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger, guarded_call, cli_boundary
ledger = BudgetLedger(sys.argv[1])
def run(args):
    with patch.object(ledger, '_write_cache', side_effect=OSError('cache only')):
        guarded_call(ledger, FakeBackend(usd='0.2', reserve_usd='0.1'), 'over', 'p', 'm', 1)
sys.exit(cli_boundary(run, None))
"""
        result = subprocess.run([sys.executable, "-c", source, str(self.path)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("reserved=0.1 actual=0.2 delta=0.1", result.stderr)
        self.assertEqual(self.ledger.status()["settled"], Decimal("0.2"))
