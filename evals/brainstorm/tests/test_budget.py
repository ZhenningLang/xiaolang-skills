import json
import tempfile
import unittest
import os
import subprocess
import sys
from types import SimpleNamespace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from evals.brainstorm.backends import CallResult, FakeBackend, NotStarted
from evals.brainstorm.budget import (BudgetExceeded, BudgetLedger, CallBusy, CallUncertain,
                                     LedgerError, PaidCallError, guarded_call)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.jsonl"
        self.ledger = BudgetLedger(self.path, limit="0.3")

    def call(self, key="one", backend=None, ledger=None):
        return guarded_call(ledger or self.ledger, backend or self.backend, key, "prompt", "m", 5)

    def test_preflight_decimal_limit_and_cache(self):
        self.backend = FakeBackend(usd="0.1", reserve_usd="0.1")
        result = self.call()
        prefix = self.path.read_bytes()
        self.assertEqual(self.call(), result)
        self.assertEqual(self.path.read_bytes(), prefix)
        self.call("two")
        self.call("three")
        with self.assertRaises(BudgetExceeded):
            self.call("four")
        self.assertEqual(self.backend.calls, 3)
        self.assertEqual(self.ledger.status()["used"], Decimal("0.3"))
        self.assertEqual(BudgetLedger(self.path).limit, Decimal(50))
        self.assertEqual([e["event"] for e in map(json.loads, self.path.read_text().splitlines())],
                         ["reserve", "settle"] * 3)

    def test_decimal_near_boundary_never_rounds_down(self):
        ledger = BudgetLedger(self.path, "0.300000000000000000000000001")
        backend = FakeBackend(usd="0.1", reserve_usd="0.1")
        self.call("a", backend, ledger)
        self.call("b", backend, ledger)
        backend.reserve_usd = Decimal("0.100000000000000000000000002")
        with self.assertRaises(BudgetExceeded):
            self.call("c", backend, ledger)
        self.assertEqual(backend.calls, 2)

    def test_sub_context_precision_cost_cannot_bypass_limit(self):
        ledger = BudgetLedger(self.path, "0.3")
        backend = FakeBackend(usd="0.2", reserve_usd="0.2")
        self.call("a", backend, ledger)
        backend.reserve_usd = Decimal("0.100000000000000000000000000000000001")
        with self.assertRaises(BudgetExceeded):
            self.call("b", backend, ledger)
        self.assertEqual(backend.calls, 1)

    def test_paid_error_charged_without_success_cache(self):
        backend = FakeBackend(failure="paid_error")
        with self.assertRaises(PaidCallError):
            self.call(backend=backend)
        self.assertFalse(self.ledger.cache_path("one").exists())
        prefix = self.path.read_bytes()
        with self.assertRaises(PaidCallError):
            self.call(backend=backend)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(self.path.read_bytes(), prefix)
        self.assertEqual(self.ledger.status()["settled"], Decimal("0.01"))
        with self.assertRaises(LedgerError):
            self.ledger.release("one", confirm_not_charged=True)
        with self.assertRaises(PaidCallError):
            self.call("attempt_2", backend)
        self.assertEqual(backend.calls, 2)

    def test_not_started_releases_and_same_key_can_retry(self):
        backend = FakeBackend(failure="not_started")
        with self.assertRaises(NotStarted):
            self.call(backend=backend)
        prefix = self.path.read_bytes()
        self.ledger.release("one", confirm_not_charged=True)
        self.assertEqual(self.path.read_bytes(), prefix)
        self.assertEqual(self.ledger.status()["used"], 0)
        backend.failure = None
        self.call(backend=backend)
        self.assertEqual(backend.calls, 1)

    def test_unknown_retains_single_reservation_until_reconciled(self):
        backend = FakeBackend(failure="unknown")
        with self.assertRaisesRegex(CallUncertain, "ledger settle"):
            self.call(backend=backend)
        self.assertEqual(self.ledger.status()["used"], Decimal("0.02"))
        self.assertEqual(self.ledger.status()["uncertain"], Decimal("0.02"))
        with self.assertRaises(CallUncertain):
            self.call(backend=backend)
        self.assertEqual(backend.calls, 1)
        prefix = self.path.read_bytes()
        self.ledger.settle("one", "0.01")
        self.assertTrue(self.path.read_bytes().startswith(prefix))
        self.assertEqual(self.ledger.status()["used"], Decimal("0.01"))
        prefix = self.path.read_bytes()
        self.ledger.settle("one", "0.01")
        self.assertEqual(self.path.read_bytes(), prefix)

    def test_missing_cost_is_estimated_and_cache_preserves_it(self):
        backend = FakeBackend(failure="missing_cost")
        self.call(backend=backend)
        result = self.call(backend=backend)
        self.assertIsNone(result.usd)
        self.assertEqual(self.ledger.status()["estimated"], Decimal("0.02"))
        self.assertEqual(self.ledger.status()["settled"], Decimal("0.02"))
        self.assertEqual(backend.calls, 1)

    def test_failed_result_without_cost_remains_uncertain(self):
        backend = FakeBackend(usd=None, failure="paid_error")
        with self.assertRaises(CallUncertain):
            self.call(backend=backend)
        self.assertEqual(self.ledger.status()["uncertain"], Decimal("0.02"))
        self.assertEqual(self.ledger.status()["settled"], 0)

    def test_over_reservation_halts_persistently_even_under_total_limit(self):
        backend = FakeBackend(usd="0.04", reserve_usd="0.02")
        with self.assertRaisesRegex(BudgetExceeded, "reserved=0.02 actual=0.04 delta=0.02"):
            self.call(backend=backend)
        self.assertEqual(self.ledger.status()["settled"], Decimal("0.04"))
        self.assertEqual(len(self.ledger.status()["overruns"]), 1)
        with self.assertRaises(BudgetExceeded):
            self.call("other_run", backend, BudgetLedger(self.path, "50"))
        self.assertEqual(backend.calls, 1)

    def test_known_cost_with_invalid_text_is_still_settled(self):
        backend = FakeBackend()
        with patch.object(backend, "call", return_value=CallResult(None, Decimal("0.01"), {})):
            with self.assertRaises(PaidCallError):
                self.call(backend=backend)
        self.assertEqual(self.ledger.status()["settled"], Decimal("0.01"))
        self.assertEqual(self.ledger.status()["uncertain"], 0)

    def test_cache_corruption_and_key_mismatch_never_resend(self):
        backend = FakeBackend()
        self.call(backend=backend)
        path = self.ledger.cache_path("one")
        original = path.read_text()
        for raw in ("{", original.replace('"call_key":"one"', '"call_key":"wrong"'),
                    original.replace('"usd":0.01', '"usd":0.001')):
            path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(LedgerError):
                self.call(backend=backend)
        self.assertEqual(backend.calls, 1)

    def test_live_owner_refuses_reentry_or_manual_reconciliation(self):
        self.ledger.reserve("one", "0.1")
        for operation in (lambda: self.ledger.reserve("one", "0.1"),
                          lambda: self.ledger.release("one", confirm_not_charged=True),
                          lambda: self.ledger.settle("one", "0.01")):
            with self.assertRaises(CallBusy):
                operation()

    def test_invalid_money_and_ledger_fail_closed(self):
        backend = FakeBackend()
        for value in ("-1", "NaN", "Infinity", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.ledger.reserve("one", value)
        self.assertFalse(self.path.exists())
        valid = {"event": "reserve", "call_key": "one", "usd": 0.01, "tokens": {},
                 "estimated": False, "pid": os.getpid(), "ts": "now"}
        malformed = [b'{', b'{}\n', b'\xff\n', b'[]\n', b'\n',
                     (json.dumps(valid) + "\n") .encode() * 2,
                     (json.dumps(dict(valid, event="settle", outcome="ok")) + "\n").encode(),
                     (json.dumps(dict(valid, usd=float("nan"))) + "\n").encode()]
        for raw in malformed:
            self.path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(LedgerError):
                self.call(backend=backend)
            self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(backend.calls, 0)

    def test_empty_existing_ledger_is_corruption(self):
        self.path.touch()
        backend = FakeBackend()
        with self.assertRaisesRegex(LedgerError, "Empty"):
            self.call(backend=backend)
        self.assertEqual(backend.calls, 0)
        self.assertEqual(self.path.read_bytes(), b"")

    def test_missing_ledger_with_surviving_cache_never_resets_budget(self):
        backend = FakeBackend(usd="0.3", reserve_usd="0.3")
        self.call(backend=backend)
        self.path.unlink()
        with self.assertRaisesRegex(LedgerError, "Missing"):
            self.call("new", backend)
        self.assertEqual(backend.calls, 1)
        self.assertFalse(self.path.exists())

    def test_zero_reservation_never_starts_backend(self):
        backend = FakeBackend(usd=None, reserve_usd="0")
        with self.assertRaisesRegex(ValueError, "positive.*price"):
            self.call(backend=backend)
        self.assertEqual(backend.calls, 0)
        self.assertFalse(self.path.exists())

    def test_cache_write_failure_still_records_known_cost(self):
        backend = FakeBackend(usd="0.1", reserve_usd="0.2")
        with patch.object(self.ledger, "_write_cache", side_effect=PermissionError("cache only")):
            with self.assertRaises(PaidCallError):
                self.call(backend=backend)
        self.assertEqual(self.ledger.status()["settled"], Decimal("0.1"))
        self.assertFalse(self.ledger.cache_path("one").exists())
        with self.assertRaises(PaidCallError):
            self.call(backend=backend)
        self.assertEqual(backend.calls, 1)

    def test_cache_failure_overrun_is_settled_and_persistently_stops(self):
        backend = FakeBackend(usd="0.2", reserve_usd="0.1")
        with patch.object(self.ledger, "_write_cache", side_effect=OSError("cache full")):
            with self.assertRaisesRegex(BudgetExceeded, "reserved=0.1 actual=0.2 delta=0.1"):
                self.call(backend=backend)
        self.assertEqual(self.ledger.status()["settled"], Decimal("0.2"))
        self.assertEqual(len(self.ledger.status()["overruns"]), 1)
        self.assertFalse(self.ledger.cache_path("one").exists())
        with self.assertRaises(BudgetExceeded):
            self.call("new", backend, BudgetLedger(self.path))
        self.assertEqual(backend.calls, 1)

    def test_cache_failure_after_replace_does_not_leave_success_result(self):
        backend = FakeBackend()
        write_cache = self.ledger._write_cache

        def fail_after_replace(*args):
            write_cache(*args)
            raise OSError("cache directory fsync failed")

        with patch.object(self.ledger, "_write_cache", side_effect=fail_after_replace):
            with self.assertRaises(PaidCallError):
                self.call(backend=backend)
        self.assertFalse(self.ledger.cache_path("one").exists())
        self.assertEqual(self.ledger.status()["settled"], Decimal("0.01"))
        with self.assertRaises(PaidCallError):
            self.call(backend=backend)
        self.assertEqual(backend.calls, 1)

    def test_invalid_raw_reference_is_paid_error_not_bad_cache(self):
        for reference in (Path("raw.json"), 123):
            with self.subTest(reference=reference):
                key = str(reference)
                backend = FakeBackend()
                with patch.object(backend, "call", return_value=CallResult("ok", Decimal("0.01"), {}, reference)):
                    with self.assertRaises(PaidCallError):
                        self.call(key, backend)
                self.assertFalse(self.ledger.cache_path(key).exists())
        self.assertEqual(self.ledger.status()["settled"], Decimal("0.02"))

    def test_unknown_cost_and_cache_failure_is_uncertain(self):
        backend = FakeBackend(usd=None)
        with patch.object(self.ledger, "_write_cache", side_effect=OSError("cache full")):
            with self.assertRaises(CallUncertain):
                self.call(backend=backend)
        self.assertEqual(self.ledger.status()["uncertain"], Decimal("0.02"))
        self.assertEqual(self.ledger.status()["settled"], 0)

    def test_failed_settlement_durably_blocks_other_calls(self):
        # A fatal billing failure is process-scoped; exercise it in a disposable child.
        source = """import json, sys
from unittest.mock import patch
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger, guarded_call
ledger = BudgetLedger(sys.argv[1])
backend = FakeBackend(usd='0.2', reserve_usd='0.1')
append = ledger._append
def fail(kind, *args, **kwargs):
    if kind == 'settle':
        raise OSError('ledger full')
    return append(kind, *args, **kwargs)
with patch.object(ledger, '_append', side_effect=fail):
    try:
        guarded_call(ledger, backend, 'one', 'p', 'm', 1)
    except Exception as exc:
        print(type(exc).__name__, str(exc))
try:
    guarded_call(BudgetLedger(sys.argv[1]), backend, 'new', 'p', 'm', 1)
except Exception as exc:
    print(type(exc).__name__, str(exc))
print(json.dumps({'calls': backend.calls, 'evidence': ledger.lock_path.read_text()}))
"""
        result = subprocess.run([sys.executable, "-c", source, str(self.path)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("persist", result.stdout)
        self.assertIn("halt", result.stdout)
        observed = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(observed["calls"], 1)
        self.assertIn('"usd":0.2', observed["evidence"])

    def test_non_dataclass_result_with_known_cost_is_settled(self):
        backend = FakeBackend(reserve_usd="0.1")
        result = SimpleNamespace(text="bad protocol", usd=Decimal("0.9"), tokens={},
                                 raw_ref=None, outcome="ok")
        with patch.object(backend, "call", return_value=result):
            with self.assertRaisesRegex(BudgetExceeded, "actual=0.9 delta=0.8"):
                self.call(backend=backend)
        self.assertEqual(self.ledger.status()["settled"], Decimal("0.9"))
        self.assertFalse(self.ledger.cache_path("one").exists())
        with self.assertRaises(BudgetExceeded):
            self.call("new", backend)

    def test_unhashable_outcome_keeps_known_fee(self):
        backend = FakeBackend(reserve_usd="0.1")
        for index, outcome in enumerate(([], {}, set())):
            with self.subTest(outcome=outcome):
                ledger = BudgetLedger(Path(self.temp.name) / f"shape_{index}.jsonl")
                result = CallResult("bad protocol", Decimal("0.9"), outcome=outcome)
                with patch.object(backend, "call", return_value=result):
                    with self.assertRaisesRegex(BudgetExceeded, "actual=0.9 delta=0.8"):
                        self.call("paid", backend, ledger)
                self.assertEqual(ledger.status()["settled"], Decimal("0.9"))
                self.assertFalse(ledger.cache_path("paid").exists())

    def test_all_storage_writes_fail_process_stops_even_after_storage_recovers(self):
        source = """import json, resource, signal, sys
from pathlib import Path
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger, guarded_call
original = resource.getrlimit(resource.RLIMIT_FSIZE)
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
class FullDisk(FakeBackend):
    def call(self, *args):
        result = super().call(*args)
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, original[1]))
        return result
backend = FullDisk(usd='0.9', reserve_usd='0.1')
first = None
try:
    guarded_call(BudgetLedger(sys.argv[1], '0.5'), backend, 'paid', 'p', 'm', 1)
except Exception as exc:
    first = {'type': type(exc).__name__, 'message': str(exc)}
finally:
    resource.setrlimit(resource.RLIMIT_FSIZE, original)
follow = FakeBackend(usd='0.1', reserve_usd='0.1')
errors = []
for path in (Path(sys.argv[1]), Path(sys.argv[1]).with_name('different.jsonl')):
    try:
        guarded_call(BudgetLedger(path), follow, 'new', 'p', 'm', 1)
    except Exception as exc:
        errors.append(type(exc).__name__)
print(json.dumps({'first': first, 'calls': follow.calls, 'errors': errors,
                  'first_calls': backend.calls}))
"""
        result = subprocess.run([sys.executable, "-c", source, str(self.path)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["first"]["type"], "BudgetExceeded")
        self.assertRegex(observed["first"]["message"], "paid.*reserved=0.1 actual=0.9 delta=0.8")
        self.assertEqual(observed["first_calls"], 1)
        self.assertEqual(observed["calls"], 0)
        self.assertEqual(len(observed["errors"]), 2)

    def test_manual_zero_settlement_requires_confirmed_release_instead(self):
        with self.assertRaises(CallUncertain):
            self.call(backend=FakeBackend(failure="unknown"))
        original = self.path.read_bytes()
        with self.assertRaisesRegex(LedgerError, "release.*confirm-not-charged"):
            self.ledger.settle("one", "0")
        self.assertEqual(self.path.read_bytes(), original)
        self.ledger.release("one", confirm_not_charged=True)
        self.assertEqual(self.ledger.status()["used"], 0)

    def test_unrepresentable_settlement_rejected_before_ledger_changes(self):
        with self.assertRaises(CallUncertain):
            self.call(backend=FakeBackend(failure="unknown"))
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.ledger.settle("one", "1e20000000")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.ledger.status()["used"], Decimal("0.02"))
