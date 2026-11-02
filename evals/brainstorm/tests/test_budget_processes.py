"""Real process boundaries, with event barriers rather than timing guesses."""

import multiprocessing
import fcntl
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import (BudgetExceeded, BudgetLedger, CallUncertain, LedgerError,
                                     default_ledger_path, guarded_call)
from evals.brainstorm import budget


def worker(path, key, count, ready, proceed, output, *, start=None, cut=None):
    class ObservedLedger(BudgetLedger):
        def reserve(self, *args):
            result = super().reserve(*args)
            if cut == "reserve":
                ready.set()
                proceed.wait(20)
            return result

        def _write_cache(self, *args):
            if cut == "before_cache":
                ready.set()
                proceed.wait(20)
            super()._write_cache(*args)
            if cut == "after_cache":
                ready.set()
                proceed.wait(20)

    class ObservedBackend(FakeBackend):
        def call(self, *args):
            with count.get_lock():
                count.value += 1
            result = super().call(*args)
            if cut is None:
                ready.set()
                proceed.wait(20)
            return result

    try:
        if start is not None:
            start.wait(20)
        result = guarded_call(ObservedLedger(path, "0.2"),
                              ObservedBackend(usd="0.2", reserve_usd="0.2"), key, "p", "m", 1)
        output.put(("ok", result.text))
    except Exception as exc:
        output.put((type(exc).__name__, str(exc)))


def settlement_failure_worker(path, role, count, ready, proceed, output):
    ledger = BudgetLedger(path, "0.3")

    class Counted(FakeBackend):
        def call(self, *args):
            with count.get_lock():
                count.value += 1
            return super().call(*args)

    def call():
        try:
            guarded_call(ledger, Counted(usd="0.2", reserve_usd="0.1" if role == "first" else "0.2"),
                         role, "p", "m", 1)
            output.put((role, "ok"))
        except Exception as exc:
            output.put((role, type(exc).__name__))

    if role == "second":
        # A nonblocking OS-lock probe is deterministic, unlike a sleep-based negative assertion.
        with ledger.lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                output.put(("probe", True))
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
                output.put(("probe", False))
        call()
        return

    actual_open, actual_flock, actual_write = Path.open, fcntl.flock, budget.atomic_write
    failed = marked = False

    class SlowLock:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, value):
            nonlocal marked
            ready.set()
            if not proceed.wait(10):
                raise TimeoutError("halt write barrier")
            result = self.stream.write(value)
            marked = True
            return result

    def open_file(path, *args, **kwargs):
        stream = actual_open(path, *args, **kwargs)
        return SlowLock(stream) if path == ledger.lock_path else stream

    def write_file(path, text):
        nonlocal failed
        if path.parent == ledger.cache_dir:
            raise PermissionError("cache failure")
        if path == ledger.path and json.loads(text.splitlines()[-1])["event"] == "settle":
            failed = True
            raise OSError("settle failure")
        return actual_write(path, text)

    def flock(lock, operation):
        result = actual_flock(lock, operation)
        if operation == fcntl.LOCK_UN and failed and not marked:
            # Old code first unlocks here; pause before its separate halt can reacquire.
            ready.set()
            if not proceed.wait(10):
                raise TimeoutError("unprotected failure barrier")
        return result

    with patch.object(Path, "open", open_file), patch.object(budget, "atomic_write", write_file), \
            patch.object(fcntl, "flock", flock):
        call()


def owner_dies_during_paid_call(path, role, count, ready, proceed, output):
    ledger = BudgetLedger(path, "0.5")
    if role == "victim":
        ledger.reserve("dead", "0.05")
        ready.set()
        proceed.wait(20)
        return

    class Counted(FakeBackend):
        def call(self, *args):
            with count.get_lock():
                count.value += 1
            if role == "paid":
                ready.set()
                if not proceed.wait(20):
                    raise TimeoutError("paid call barrier")
            return super().call(*args)

    try:
        guarded_call(ledger, Counted(usd="0.9" if role == "paid" else "0.1", reserve_usd="0.1"),
                     role, "p", "m", 1)
        output.put((role, "ok"))
    except Exception as exc:
        output.put((role, type(exc).__name__))


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "shared.jsonl"
        self.context = multiprocessing.get_context("spawn")
        self.count = self.context.Value("i", 0)
        self.ready = self.context.Event()
        self.proceed = self.context.Event()
        self.output = self.context.Queue()
        self.addCleanup(self.output.close)

    def launch(self, key, **kwargs):
        process = self.context.Process(target=worker, args=(str(self.path), key, self.count,
                                      self.ready, self.proceed, self.output), kwargs=kwargs)
        process.start()
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.is_alive():
            process.kill()
        process.join(5)

    def test_different_keys_compete_for_one_reservation(self):
        start = self.context.Event()
        first = self.launch("a", start=start)
        second = self.launch("b", start=start)
        start.set()
        self.assertTrue(self.ready.wait(10), "backend never started")
        rejected, _ = self.output.get(timeout=10)
        self.assertEqual(rejected, "BudgetExceeded")
        self.assertEqual(self.count.value, 1)
        self.assertEqual(BudgetLedger(self.path).status()["used"], self._usd("0.2"))
        self.proceed.set()
        self.assertEqual(self.output.get(timeout=10)[0], "ok")
        for process in (first, second):
            process.join(10)
            self.assertEqual(process.exitcode, 0)

    def test_same_key_has_live_owner_mutex(self):
        first = self.launch("same")
        self.assertTrue(self.ready.wait(10))
        second = self.launch("same")
        self.assertEqual(self.output.get(timeout=10)[0], "CallBusy")
        self.assertEqual(self.count.value, 1)
        self.proceed.set()
        self.assertEqual(self.output.get(timeout=10)[0], "ok")
        for process in (first, second):
            process.join(10)
            self.assertEqual(process.exitcode, 0)

    @staticmethod
    def _usd(value):
        return Decimal(value)

    def test_three_crash_points_never_resend_or_double_charge(self):
        for cut, expected_count in (("reserve", 0), ("before_cache", 1), ("after_cache", 1)):
            with self.subTest(cut=cut):
                self.path = Path(self.temp.name) / f"{cut}.jsonl"
                self.ready.clear()
                self.count.value = 0
                process = self.launch("same", cut=cut)
                self.assertTrue(self.ready.wait(10), f"did not reach {cut}")
                process.kill()
                process.join(10)
                self.assertIsNotNone(process.exitcode)
                ledger = BudgetLedger(self.path)
                backend = FakeBackend(usd="0.2", reserve_usd="0.2")
                prefix = self.path.read_bytes()
                for recovery in range(2):
                    if cut == "after_cache":
                        result = guarded_call(ledger, backend, "same", "p", "m", 1)
                        self.assertEqual(result.usd, self._usd("0.2"))
                    else:
                        with self.assertRaises(CallUncertain):
                            guarded_call(ledger, backend, "same", "p", "m", 1)
                    self.assertEqual(ledger.status()["used"], self._usd("0.2"))
                    self.assertTrue(self.path.read_bytes().startswith(prefix))
                    prefix = self.path.read_bytes()
                self.assertEqual(backend.calls, 0)
                self.assertEqual(self.count.value, expected_count)
                events = self.path.read_text().splitlines()
                self.assertEqual(len(events), 2)
                self.assertIn('"event":"settle"' if cut == "after_cache" else '"event":"uncertain"', events[-1])

    def test_shared_ledger_across_run_directories_and_fake_real_isolation(self):
        first_run = Path(self.temp.name) / "run_a"
        second_run = Path(self.temp.name) / "run_b"
        first_run.mkdir()
        second_run.mkdir()
        backend = FakeBackend(usd="0.2", reserve_usd="0.2")
        first = BudgetLedger(self.path, "0.2")
        result = guarded_call(first, backend, "shared", str(first_run), "m", 1)
        other = BudgetLedger(self.path, "0.2")
        self.assertEqual(guarded_call(other, backend, "shared", str(second_run), "m", 1), result)
        with self.assertRaises(BudgetExceeded):
            guarded_call(other, backend, "new", "p", "m", 1)
        self.assertEqual(backend.calls, 1)
        fake = default_ledger_path("fake", first_run)
        real = default_ledger_path("claude", second_run, repo_root=self.temp.name)
        self.assertNotEqual(fake, real)
        self.assertEqual(real, default_ledger_path("claude", first_run, repo_root=self.temp.name))
        guarded_call(BudgetLedger(fake), backend, "fake", "p", "m", 1)
        self.assertFalse(real.exists())
        self.assertEqual(BudgetLedger(real).status()["used"], 0)

    def test_settlement_failure_marks_halt_before_unlocking_for_another_process(self):
        processes = []
        for role in ("first", "second"):
            process = self.context.Process(target=settlement_failure_worker,
                args=(str(self.path), role, self.count, self.ready, self.proceed, self.output))
            process.start()
            self.addCleanup(self.stop, process)
            processes.append(process)
            if role == "first":
                self.assertTrue(self.ready.wait(10), "first never reached billing failure")
        try:
            kind, held = self.output.get(timeout=10)
            self.assertEqual(kind, "probe")
            early_second = None if held else self.output.get(timeout=10)
        finally:
            self.proceed.set()
        results = [self.output.get(timeout=10)]
        results.append(early_second if early_second is not None else self.output.get(timeout=10))
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertTrue(held, f"another process acquired the lock before halt: {results}")
        self.assertEqual(self.count.value, 1)
        self.assertNotIn(("second", "ok"), results)
        self.assertIn('"usd":0.2', self.path.with_name(self.path.name + ".lock").read_text())
        with self.assertRaisesRegex(LedgerError, "halt"):
            BudgetLedger(self.path).status()

    def test_dead_other_owner_does_not_bypass_automatic_settlement_halt(self):
        directory = Path(self.temp.name) / "billing"
        directory.mkdir()
        self.path = directory / "ledger.jsonl"

        def launch(role, ready, proceed):
            process = self.context.Process(target=owner_dies_during_paid_call,
                args=(str(self.path), role, self.count, ready, proceed, self.output))
            process.start()
            self.addCleanup(self.stop, process)
            return process

        paid = launch("paid", self.ready, self.proceed)
        self.assertTrue(self.ready.wait(10), "paid request never became in-flight")
        victim_ready = self.context.Event()
        victim_proceed = self.context.Event()
        victim = launch("victim", victim_ready, victim_proceed)
        self.assertTrue(victim_ready.wait(10))
        victim.kill()
        victim.join(10)
        try:
            # Existing lock remains writable; atomic ledger/cache replacements cannot succeed.
            directory.chmod(0o500)
            self.proceed.set()
            self.assertEqual(self.output.get(timeout=10), ("paid", "BudgetExceeded"))
            paid.join(10)
            self.assertEqual(paid.exitcode, 0)
        finally:
            directory.chmod(0o700)
        next_ready, next_proceed = self.context.Event(), self.context.Event()
        next_process = launch("next", next_ready, next_proceed)
        self.assertEqual(self.output.get(timeout=10), ("next", "LedgerError"))
        next_process.join(10)
        self.assertEqual(next_process.exitcode, 0)
        self.assertEqual(self.count.value, 1)
        evidence = json.loads(self.path.with_name(self.path.name + ".lock").read_text())
        self.assertEqual(evidence["call_key"], "paid")
        self.assertEqual(evidence["usd"], 0.9)
