from decimal import Decimal
import io
import json
import sys
import unittest
from unittest.mock import patch

from evals.brainstorm.backends import CommandBackend, ResultUnknown


def command_options(**overrides):
    return {"provider": "test_provider", "argv": [sys.executable, "-c", ""],
            "pricing": {"m": {"fixed_tokens": 10, "input_per_million": 2, "output_per_million": 4}},
            "max_usd_per_call": ".02", "timeout": 2, "tools_disabled": True,
            "outbound_ack": True, **overrides}


class CommandTests(unittest.TestCase):
    def test_missing_tokens_preserves_nonempty_body_and_known_fee(self):
        body = '{"INJECTION\\u005fCANARY_42":true}'
        payload = {"text": body, "usd": .01}
        backend = CommandBackend(**command_options(argv=[sys.executable, "-c", f"print({json.dumps(payload)!r})"]))
        with patch("sys.stderr", new_callable=io.StringIO):
            result = backend.call("Question", "m", 10)
        self.assertEqual(result.text, body)
        self.assertEqual(result.outcome, "paid_error")
        self.assertEqual(result.usd, Decimal(".01"))
        self.assertEqual(result.tokens, {})

    def test_confirmation_fields_are_strict_and_no_process_starts(self):
        for key in ("tools_disabled", "outbound_ack"):
            for value in (False, None, "true", 1):
                with self.subTest(key=key, value=value), patch("subprocess.Popen") as start:
                    with self.assertRaisesRegex(ValueError, key):
                        CommandBackend(**command_options(**{key: value}))
                    start.assert_not_called()

    def test_estimate_is_maximum_of_price_and_declared_reserve(self):
        backend = CommandBackend(**command_options())
        self.assertEqual(backend.estimate_max_usd("abc", "m", 10), Decimal(".02"))
        self.assertEqual(backend.estimate_max_usd("abc", "m", 10000), Decimal(".040026"))
        with self.assertRaisesRegex(ValueError, "No configured price"):
            backend.estimate_max_usd("", "unknown", 10)
        for options in ({"max_usd_per_call": 0}, {"timeout": float("inf")}, {"timeout": True},
                        {"pricing": {"m": {"fixed_tokens": 0, "input_per_million": -1, "output_per_million": 1}}}):
            with self.assertRaises(ValueError):
                CommandBackend(**command_options(**options))

    def test_nonzero_exit_keeps_billing_and_missing_success_cost_is_not_zero(self):
        for cost, code, expected in ((.01, 1, "paid_error"), (None, 0, "ok")):
            payload = {"text": "response", "usd": cost, "tokens": {"input": 1, "cache_write": 0, "cache_read": 0, "output": 2}}
            script = f"import sys; print({json.dumps(payload)!r}); sys.exit({code})"
            backend = CommandBackend(**command_options(argv=[sys.executable, "-c", script]))
            with patch("sys.stderr", new_callable=io.StringIO):
                result = backend.call("question", "m", 1)
            self.assertEqual(result.outcome, expected)
            self.assertEqual(result.usd, None if cost is None else Decimal(".01"))

    def test_started_timeout_bad_billing_and_bad_envelope_are_unknown(self):
        for script in ("import time; time.sleep(2)", "print('not json')", "print('[]')",
                       "print('{\"usd\":-1}')", "print('{\"text\":null}')"):
            backend = CommandBackend(**command_options(argv=[sys.executable, "-c", script], timeout=.1))
            with self.subTest(script=script), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(ResultUnknown):
                backend.call("question", "m", 1)

    def test_missing_executable_is_confirmed_not_started(self):
        from evals.brainstorm.backends import NotStarted
        backend = CommandBackend(**command_options(argv=["nonexistent_test_executable_52"]))
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(NotStarted):
            backend.call("question", "m", 1)

    def test_fake_process_receives_stdin_and_never_shell_interprets_prompt(self):
        script = "import sys,json; p=sys.stdin.read(); print(json.dumps({'text':p,'usd':.01,'tokens':{'input':1,'cache_write':2,'cache_read':3,'output':4}}))"
        backend = CommandBackend(**command_options(argv=[sys.executable, "-c", script]))
        with patch("sys.stderr", new_callable=io.StringIO) as warning:
            result = backend.call("$(false); 'quoted'\nquestion", "m", 10)
        self.assertEqual(result.text, "$(false); 'quoted'\nquestion")
        self.assertEqual(result.usd, Decimal(".01"))
        self.assertEqual(result.tokens["cache_read"], 3)
        self.assertIn("test_provider", warning.getvalue())
        self.assertIn("cards", warning.getvalue())
