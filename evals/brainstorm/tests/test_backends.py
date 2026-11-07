import unittest
import json
import os
import subprocess
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from evals.brainstorm.backends import ClaudeCliBackend, FakeBackend, NotStarted, ResultUnknown
from evals.brainstorm.budget import BudgetLedger, CallUncertain, guarded_call


class FakeTests(unittest.TestCase):
    def test_deterministic_result_and_configured_cost(self):
        backend = FakeBackend(usd="0.12", reserve_usd="0.2")
        first = backend.call("prompt", "test", 10)
        self.assertEqual(first, backend.call("prompt", "test", 10))
        self.assertEqual(first.usd, Decimal("0.12"))
        self.assertEqual(backend.estimate_max_usd("prompt", "test", 10), Decimal("0.2"))
        self.assertEqual(backend.calls, 2)

    def test_injected_failures(self):
        with self.assertRaises(NotStarted):
            FakeBackend(failure="not_started").call("p", "m", 1)
        with self.assertRaises(ResultUnknown):
            FakeBackend(failure="unknown").call("p", "m", 1)
        self.assertEqual(FakeBackend(failure="paid_error").call("p", "m", 1).outcome, "paid_error")
        self.assertIsNone(FakeBackend(failure="missing_cost").call("p", "m", 1).usd)
        over = FakeBackend(usd="0.3", reserve_usd="0.2")
        self.assertGreater(over.call("p", "m", 1).usd, over.estimate_max_usd("p", "m", 1))


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.backend = ClaudeCliBackend(pricing={"m": {"fixed_tokens": 100, "input_per_million": "3", "output_per_million": "15"}})

    def response(self, **updates):
        payload = {"result": "answer", "is_error": False, "total_cost_usd": 0.031,
                   "usage": {"input_tokens": 1, "cache_creation_input_tokens": 2,
                             "cache_read_input_tokens": 3, "output_tokens": 4}}
        payload.update(updates)
        process = MagicMock()
        process.__enter__.return_value = process
        process.communicate.return_value = (json.dumps(payload), "")
        process.returncode = 1 if payload["is_error"] else 0
        return process

    @patch("evals.brainstorm.backends.subprocess.Popen")
    def test_stdin_isolation_and_cost_mapping(self, run):
        run.return_value = self.response()
        result = self.backend.call("full prompt\n$(not shell)", "m", 5)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["claude", "-p", "--setting-sources", "", "--strict-mcp-config",
                                    "--disable-slash-commands", "--tools", "", "--model", "m",
                                    "--output-format", "json", "--no-session-persistence"])
        self.assertEqual(run.return_value.communicate.call_args.kwargs["input"], "full prompt\n$(not shell)")
        self.assertEqual(kwargs["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "5")
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(result.text, "answer")
        self.assertEqual(result.usd, Decimal("0.031"))
        self.assertEqual(result.tokens, {"input": 1, "cache_write": 2, "cache_read": 3, "output": 4})

    def test_price_estimate_and_unknown_model(self):
        # 100 fixed + 2 UTF-8 bytes, then 5 output tokens: $0.000381.
        self.assertEqual(self.backend.estimate_max_usd("ab", "m", 5), Decimal("0.000381"))
        with self.assertRaisesRegex(ValueError, "price"):
            self.backend.estimate_max_usd("ab", "unknown", 5)

    @patch("evals.brainstorm.backends.subprocess.Popen")
    def test_nonzero_paid_error_and_unusable_text_keep_cost(self, run):
        for updates in ({"is_error": True, "result": None}, {"result": None}, {"usage": "broken"}):
            with self.subTest(updates=updates):
                run.return_value = self.response(**updates)
                result = self.backend.call("p", "m", 1)
                self.assertEqual(result.outcome, "paid_error")
                self.assertEqual(result.usd, Decimal("0.031"))

    @patch("evals.brainstorm.backends.subprocess.Popen")
    def test_reply_cut_at_the_output_limit_is_a_paid_error(self, run):
        # Real call, 2026-11-07: 5146 output tokens against a 4096 cap; the CLI continued and returned only the tail.
        for updates in ({"usage": {"input_tokens": 1, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3,
                                   "output_tokens": 5}}, {"num_turns": 2}):
            with self.subTest(updates=updates):
                run.return_value = self.response(**updates)
                result = self.backend.call("p", "m", 5)
                self.assertEqual(result.outcome, "paid_error")
                self.assertEqual(result.usd, Decimal("0.031"))
        run.return_value = self.response(num_turns=1)
        self.assertEqual(self.backend.call("p", "m", 5).outcome, "ok")

    @patch("evals.brainstorm.backends.subprocess.Popen")
    def test_missing_cost_is_not_zero(self, run):
        run.return_value = self.response(total_cost_usd=None)
        self.assertIsNone(self.backend.call("p", "m", 100).usd)

    @patch("evals.brainstorm.backends.subprocess.Popen")
    def test_not_started_distinct_from_unknown(self, run):
        run.side_effect = FileNotFoundError("missing")
        with self.assertRaises(NotStarted):
            self.backend.call("p", "m", 1)
        run.side_effect = None
        run.return_value = self.response()
        run.return_value.communicate.side_effect = subprocess.TimeoutExpired("claude", 1)
        with self.assertRaises(ResultUnknown):
            self.backend.call("p", "m", 1)
        for raw in ("{", "[]", "{\"total_cost_usd\": NaN}", "\ufffd"):
            run.return_value = self.response()
            run.return_value.communicate.return_value = (raw, "")
            with self.subTest(raw=raw), self.assertRaises(ResultUnknown):
                self.backend.call("p", "m", 1)

    @patch("evals.brainstorm.backends.subprocess.Popen")
    def test_parent_output_cap_is_overridden(self, popen):
        popen.return_value = self.response()
        original = os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS")
        with patch.dict(os.environ, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "999999"}):
            self.backend.call("p", "m", 256)
        self.assertEqual(popen.call_args.kwargs["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "256")
        self.assertEqual(os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), original)

    @patch("evals.brainstorm.backends.subprocess.Popen")
    def test_missing_usage_is_not_fabricated_as_zero(self, popen):
        popen.return_value = self.response(usage={})
        result = self.backend.call("p", "m", 1)
        self.assertEqual(result.tokens, {})
        self.assertEqual(result.outcome, "paid_error")
        self.assertEqual(result.usd, Decimal("0.031"))

    def test_real_child_started_before_communication_oserror_keeps_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "fake_model"
            shutil.copyfile(Path(__file__).parent / "fixtures/fake_claude.py", executable)
            executable.chmod(0o700)
            backend = ClaudeCliBackend(pricing=self.backend.pricing, executable=str(executable))
            ledger = BudgetLedger(Path(directory) / "ledger.jsonl")
            actual_popen = subprocess.Popen
            observed = []

            def start(*args, **kwargs):
                child = actual_popen(*args, **kwargs)
                actual_communicate = child.communicate

                def communicate(*args, **kwargs):
                    stdout, _ = actual_communicate(*args, **kwargs)
                    observed.append((child.pid, json.loads(stdout)["result"]))
                    raise OSError("EIO after child returned")

                child.communicate = communicate
                return child

            with patch("evals.brainstorm.backends.subprocess.Popen", side_effect=start):
                with self.assertRaises(CallUncertain):
                    guarded_call(ledger, backend, "started", "prompt", "m", 5)
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][1], "prompt")
            self.assertGreater(ledger.status()["uncertain"], 0)
            self.assertEqual(ledger.status()["used"], ledger.status()["uncertain"])
            self.assertNotIn('"event":"release"', ledger.path.read_text())

    def test_actual_fake_process_stdin_paid_error_and_invalid_response(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "fake_model"
            shutil.copyfile(Path(__file__).parent / "fixtures/fake_claude.py", executable)
            executable.chmod(0o700)
            backend = ClaudeCliBackend(pricing=self.backend.pricing, executable=str(executable))
            result = backend.call("entire\nstdin prompt", "m", 5)
            self.assertEqual(result.text, "entire\nstdin prompt")
            self.assertEqual(result.usd, Decimal("0.01"))
            failed = backend.call("paid_error", "m", 5)
            self.assertEqual((failed.outcome, failed.usd), ("paid_error", Decimal("0.01")))
            with self.assertRaises(ResultUnknown):
                backend.call("invalid", "m", 5)
            backend.executable = str(Path(directory) / "missing")
            with self.assertRaises(NotStarted):
                backend.call("p", "m", 5)


if __name__ == "__main__":
    unittest.main()
