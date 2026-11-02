from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger, BudgetExceeded, CallUncertain, PaidCallError
from evals.brainstorm.config import validate_config
from evals.brainstorm.exam import load_exams
from evals.brainstorm.record import record_batch


SAMPLES = Path(__file__).resolve().parents[1] / "exams/samples"
GOOD = json.dumps({"ideas": [{"title": "Divider", "text": "Separate items with a removable divider.",
                              "cards_used": ["c1"], "decomposition": ["access", "return"]}]})


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exam = load_exams(SAMPLES / "open")[0]
        self.config = validate_config({"name": "test", "default_model": "m", "agents": 2})
        self.ledger = BudgetLedger(self.root / "ledger.fake.jsonl", "50")
        self.backend = FakeBackend(responses=[GOOD] * 6)
        self.output = self.root / "open_sample/test/rep-1"

    def record(self, **kwargs):
        return record_batch(self.exam, self.config, self.root, seed=42,
                            backend=kwargs.pop("backend", self.backend),
                            ledger=kwargs.pop("ledger", self.ledger), **kwargs)

    def rows(self, name):
        return [json.loads(s, parse_float=Decimal) for s in (self.output / name).read_text().splitlines()]

    def test_claude_shaped_null_result_cannot_certify_zero_injection(self):
        from evals.brainstorm.backends import ClaudeCliBackend
        self.exam = load_exams(SAMPLES / "injection")[0]
        self.config["agents"] = 1
        payload = {"is_error": True, "result": None, "total_cost_usd": .0088,
                   "usage": {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}}
        executable = self.root / "fake_claude"
        executable.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.read()\nprint({json.dumps(payload)!r})\nsys.exit(1)\n")
        executable.chmod(0o700)
        backend = ClaudeCliBackend(pricing={"m": {"fixed_tokens": 10, "input_per_million": 2, "output_per_million": 4}},
                                   executable=str(executable))
        summary = self.record(backend=backend)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["usd"], Decimal(".0088"))
        self.assertEqual(summary["guards"]["injection"], {"status": "not_evaluated", "violations": None})

    def test_escaped_keys_in_failed_idea_responses_keep_violation_evidence_on_resume(self):
        self.exam = load_exams(SAMPLES / "injection")[0]
        self.config["agents"] = 1
        backend = FakeBackend(responses=['{"INJECTION\\u005fCANARY_42":true}'] * 2)
        summary = self.record(backend=backend)
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["ideas"], 0)
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["usd"], Decimal(".02"))
        self.assertEqual(summary["guards"]["injection"], {"status": "checked", "violations": 2})
        fresh = FakeBackend(responses=[])
        self.assertEqual(self.record(backend=fresh)["guards"], summary["guards"])
        self.assertEqual(fresh.calls, 0)

    def test_command_missing_tokens_keeps_probe_and_partial_empty_calls_remain_unknown(self):
        from evals.brainstorm.backends import CommandBackend
        from evals.brainstorm.tests.test_command_backend import command_options
        self.exam = load_exams(SAMPLES / "injection")[0]
        self.config["agents"] = 1
        payload = {"text": '{"INJECTION\\u005fCANARY_42":true}', "usd": .01}
        backend = CommandBackend(**command_options(argv=[sys.executable, "-c", f"print({json.dumps(payload)!r})"]))
        summary = self.record(backend=backend)
        self.assertEqual(summary["usd"], Decimal(".01"))
        self.assertEqual(summary["guards"]["injection"], {"status": "checked", "violations": 1})
        self.config["agents"] = 2
        for first, expected in ((GOOD, None), (GOOD.replace("Separate items with a removable divider.", "INJECTION_CANARY_42"), 1)):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                from dataclasses import replace
                class Mixed(FakeBackend):
                    def call(self, *args):
                        result = super().call(*args)
                        return replace(result, text="", outcome="paid_error") if self.calls == 2 else result
                summary = record_batch(self.exam, self.config, tmp, seed=42, backend=Mixed(responses=[first, ""]),
                                       ledger=BudgetLedger(Path(tmp) / "ledger"))
                self.assertEqual(summary["guards"]["injection"], {"status": "not_evaluated", "violations": expected})

    def test_escaped_injection_markers_count_once_after_decode_and_resume(self):
        self.exam = load_exams(SAMPLES / "injection")[0]
        self.config["agents"] = 1
        self.output = self.root / "injection_sample/test/rep-1"
        for marker in ("INJECTION_CANARY_42", "注入标记"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                exam = {**self.exam, "planted": [{"card_id": "c1", "kind": "injection", "marker": marker}]}
                response = json.loads(GOOD)
                response["ideas"][0]["text"] = marker
                encoded = json.dumps(response, ensure_ascii=True).replace("CANARY_", "CANARY\\u005f")
                backend = FakeBackend(responses=[encoded])
                ledger = BudgetLedger(Path(tmp) / "ledger")
                for _ in range(2):
                    summary = record_batch(exam, self.config, tmp, seed=42, backend=backend, ledger=ledger)
                    self.assertEqual(summary["guards"]["injection"], {"status": "checked", "violations": 1})
                self.assertEqual(backend.calls, 1)

    def test_unexposed_injection_is_unknown_and_no_probe_is_not_applicable(self):
        summary = self.record()
        self.assertEqual(summary["guards"]["injection"], {"status": "not_applicable", "violations": None})
        self.assertEqual(summary["guards"]["constraint"], {"status": "not_evaluated", "violations": None})
        self.exam = load_exams(SAMPLES / "injection")[0]
        for mode, proportion in (("none", 1), ("proportion", .5)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                config = {**self.config, "materials_mode": mode, "materials_proportion": proportion}
                response = GOOD.replace('["c1"]', '[]')
                summary = record_batch(self.exam, config, tmp, seed=42, backend=FakeBackend(responses=[response] * 2),
                                       ledger=BudgetLedger(Path(tmp) / "ledger"))
                self.assertEqual(summary["guards"]["injection"], {"status": "not_evaluated", "violations": None})

    def test_manually_settled_unread_response_does_not_claim_full_scan(self):
        self.exam = load_exams(SAMPLES / "injection")[0]
        self.config["agents"] = 1
        with self.assertRaises(CallUncertain):
            self.record(backend=FakeBackend(failure="unknown"))
        key = next(iter(self.ledger._read()))
        self.ledger.settle(key, ".01")
        summary = self.record(backend=FakeBackend(responses=[]))
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["guards"]["injection"], {"status": "not_evaluated", "violations": None})

    def test_bad_then_good_charges_both_attempts_and_terminal_failure_does_not_retry(self):
        self.config["agents"] = 1
        backend = FakeBackend(responses=["bad JSON", GOOD])
        summary = self.record(backend=backend)
        self.assertEqual(backend.calls, 2)
        self.assertEqual(summary["usd"], Decimal(".02"))
        calls = self.rows("calls.jsonl")
        self.assertEqual([c["attempt"] for c in calls], [1, 2])
        self.assertNotEqual(calls[0]["call_key"], calls[1]["call_key"])
        self.assertEqual(self.rows("ideas.jsonl")[0]["call_key"], calls[1]["call_key"])
        self.assertEqual(self.rows("failures.jsonl"), [])
        with tempfile.TemporaryDirectory() as tmp:
            bad = FakeBackend(responses=["bad", "still bad"])
            ledger = BudgetLedger(Path(tmp) / "ledger", "1")
            for _ in range(2):
                summary = record_batch(self.exam, self.config, tmp, seed=1, backend=bad, ledger=ledger)
            self.assertEqual(bad.calls, 2)
            self.assertEqual(summary["ideas"], 0)
            self.assertEqual(summary["failures"], 1)
            self.assertEqual(summary["usd"], Decimal(".02"))

    def test_baseline_records_no_method_even_when_configuration_assigns_methods(self):
        self.config.update(method_mode="same", methods={"reverse": "METHOD_SHOULD_NOT_APPEAR"})
        observed = []
        def respond(prompt, *_):
            observed.append(prompt)
            return GOOD
        self.record(backend=FakeBackend(responses=respond), level="baseline")
        self.assertTrue(all(i["method"] is None for i in self.rows("ideas.jsonl")))
        self.assertTrue(all("METHOD_SHOULD_NOT_APPEAR" not in p for p in observed))

    def test_ledger_write_failure_never_continues_to_second_agent(self):
        from evals.brainstorm import budget
        append = self.ledger._append
        def fail_settle(kind, *args, **kwargs):
            if kind == "settle":
                raise OSError("settlement storage unavailable")
            return append(kind, *args, **kwargs)
        with patch.object(budget, "_PROCESS_FAILURE", None), patch.object(self.ledger, "_append", side_effect=fail_settle):
            with self.assertRaisesRegex(Exception, "process halted"):
                self.record()
        self.assertEqual(self.backend.calls, 1)

    def test_output_rejects_forged_identity_bad_cards_and_partial_bad_batch(self):
        from evals.brainstorm.record import parse_ideas
        row = json.loads(GOOD)["ideas"][0]
        for bad in ({**row, "idea_id": "forged"}, {**row, "cards_used": ["unassigned"]},
                    {**row, "text": ""}, {**row, "decomposition": {}},
                    {**row, "cards_used": "c1"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_ideas(json.dumps({"ideas": [row, bad]}), ["c1"])
        for text in ('{"ideas":[]}', '[]', 'null', '{"ideas":[null]}', '{"ideas":', '\x00'):
            with self.assertRaises(ValueError):
                parse_ideas(text, ["c1"])

    def test_deep_invalid_json_is_terminal_paid_parse_failure_not_crash(self):
        self.config["agents"] = 1
        backend = FakeBackend(responses=["[" * 200000] * 2)
        summary = self.record(backend=backend)
        self.assertEqual(backend.calls, 2)
        self.assertEqual(summary["ideas"], 0)
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["usd"], Decimal(".02"))
        self.record(backend=FakeBackend(responses=[]))

    def test_repeat_and_batch_context_derive_reproducible_independent_assignments(self):
        self.exam["card_data"] = [{"id": f"c{i}", "text": f"Card {i}"} for i in range(20)]
        self.config.update(agents=4, models=["m", "n"], materials_mode="proportion", materials_proportion=.3)
        response = GOOD.replace('["c1"]', '[]')
        backend = FakeBackend(responses=[response] * 12)
        first = self.record(backend=backend)
        second = self.record(backend=backend, rep=2)
        third = self.record(backend=backend, batch=2, level="pressure2")
        self.assertNotEqual(first["allocations"]["1:round1"], second["allocations"]["1:round1"])
        self.assertNotEqual(first["allocations"]["1:round1"], third["allocations"]["2:pressure2"])
        resumed = self.record(backend=FakeBackend(responses=[]))
        self.assertEqual(first["allocations"]["1:round1"], resumed["allocations"]["1:round1"])
        for invalid_seed in (True, "42", None):
            with self.subTest(seed=invalid_seed), self.assertRaisesRegex(ValueError, "seed must be an integer"):
                record_batch(self.exam, self.config, self.root / "invalid_seed", seed=invalid_seed,
                             backend=FakeBackend(responses=[]), ledger=self.ledger)

    def test_bad_output_then_budget_stop_resumes_at_attempt_two(self):
        self.config["agents"] = 1
        backend = FakeBackend(responses=["bad", GOOD])
        with self.assertRaisesRegex(BudgetExceeded, "shortfall_usd=.*pending_agents=1"):
            self.record(backend=backend, ledger=BudgetLedger(self.ledger.path, ".02"))
        self.assertEqual(backend.calls, 1)
        self.assertEqual(self.rows("ideas.jsonl"), [])
        summary = self.record(backend=backend)
        self.assertEqual(backend.calls, 2)
        self.assertEqual(summary["usd"], Decimal(".02"))
        self.assertEqual([c["attempt"] for c in self.rows("calls.jsonl")], [1, 2])
        self.record(backend=FakeBackend(responses=[]))
        self.assertEqual(self.ledger.status()["settled"], Decimal(".02"))

    def test_repeated_budget_stops_are_deduplicated_and_resolved_failures_disappear(self):
        for limit in (".02", ".025"):
            with self.assertRaises(BudgetExceeded):
                self.record(ledger=BudgetLedger(self.ledger.path, limit))
            self.assertEqual(len(self.rows("failures.jsonl")), 1)
        self.assertEqual(self.backend.calls, 1)
        summary = self.record()
        self.assertEqual(summary["failures"], 0)
        self.assertEqual(self.rows("failures.jsonl"), [])
        self.assertEqual(summary["ideas"], 2)
        self.assertEqual(self.backend.calls, 2)

    def test_recovery_after_paid_cache_before_idea_commit(self):
        with patch("evals.brainstorm.record.sync_call", side_effect=OSError("simulated crash after cache")):
            with self.assertRaises(OSError):
                self.record()
        self.assertEqual(self.backend.calls, 1)
        fresh = FakeBackend(responses=[GOOD])
        summary = self.record(backend=fresh, ledger=BudgetLedger(self.ledger.path))
        self.assertEqual(fresh.calls, 1)
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(self.ledger.status()["settled"], Decimal(".02"))
        self.assertTrue(all(c["started_at"] and c["duration_ms"] >= 0 for c in self.rows("calls.jsonl")))

    def test_recovery_after_idea_commit_before_summary_write(self):
        from evals.brainstorm.record import atomic_write
        def fail_summary(path, text):
            if Path(path).name == "run_summary.json" and self.output.joinpath("ideas.jsonl").read_text():
                raise OSError("summary unavailable")
            return atomic_write(path, text)
        with patch("evals.brainstorm.record.atomic_write", side_effect=fail_summary):
            with self.assertRaises(OSError):
                self.record()
        self.assertEqual(self.backend.calls, 1)
        fresh = FakeBackend(responses=[GOOD])
        self.assertEqual(self.record(backend=fresh)["ideas"], 2)
        self.assertEqual(fresh.calls, 1)

    def test_identity_conflicts_corruption_and_symlink_output_escape_stop(self):
        self.record()
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            record_batch(self.exam, self.config, self.root, seed=99, backend=self.backend, ledger=self.ledger)
        altered = {**self.config, "three_step": True}
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            record_batch(self.exam, altered, self.root, seed=42, backend=self.backend, ledger=self.ledger)
        (self.output / "ideas.jsonl").write_text('{"truncated":')
        with self.assertRaisesRegex(ValueError, "truncated"):
            self.record()
        self.assertEqual(self.backend.calls, 2)
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as run:
            (Path(run) / self.exam["id"]).symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "escapes"):
                record_batch(self.exam, self.config, run, seed=1, backend=self.backend, ledger=self.ledger)

    def test_paid_errors_are_terminal_and_keep_cost(self):
        backend = FakeBackend(failure="paid_error", responses=[GOOD, GOOD])
        summary = self.record(backend=backend)
        self.assertEqual(summary["ideas"], 0)
        self.assertEqual(summary["usd"], Decimal(".02"))
        self.assertTrue(all(c["outcome"] == "paid_error" for c in self.rows("calls.jsonl")))
        self.record(backend=FakeBackend(responses=[]))
        self.assertEqual(backend.calls, 2)

    def test_unknown_billing_keeps_reserve_and_stops_other_agents(self):
        backend = FakeBackend(failure="unknown")
        with self.assertRaises(CallUncertain):
            self.record(backend=backend)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(self.ledger.status()["uncertain"], Decimal(".02"))
        call = self.rows("calls.jsonl")[0]
        self.assertEqual(call["outcome"], "uncertain")
        self.assertTrue(call["usd_estimated"])
        with self.assertRaises(CallUncertain):
            self.record(backend=backend)
        self.assertEqual(backend.calls, 1)

    def test_released_uncertain_call_disappears_even_when_retry_is_not_started(self):
        from evals.brainstorm.backends import NotStarted
        with self.assertRaises(CallUncertain):
            self.record(backend=FakeBackend(failure="unknown"))
        key = next(iter(self.ledger._read()))
        self.ledger.release(key, confirm_not_charged=True)
        with self.assertRaises(NotStarted):
            self.record(backend=FakeBackend(failure="not_started"))
        summary = json.loads((self.output / "run_summary.json").read_text())
        self.assertEqual(summary["usd"], 0)
        self.assertEqual(summary["calls"], 0)
        self.assertEqual(self.rows("calls.jsonl"), [])
        self.assertEqual(self.ledger.status()["used"], 0)

    def test_released_call_projection_recovers_if_export_is_interrupted(self):
        from evals.brainstorm.backends import NotStarted
        from evals.brainstorm.record import export_outputs
        with self.assertRaises(CallUncertain):
            self.record(backend=FakeBackend(failure="unknown"))
        self.ledger.release(next(iter(self.ledger._read())), confirm_not_charged=True)
        def interrupted(path, state, exam):
            if state.get("released_call_history"):
                raise OSError("projection interrupted after release")
            return export_outputs(path, state, exam)
        with patch("evals.brainstorm.record.export_outputs", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.record(backend=FakeBackend(failure="not_started"))
        with self.assertRaises(NotStarted):
            self.record(backend=FakeBackend(failure="not_started"))
        self.assertEqual(self.rows("calls.jsonl"), [])
        self.assertEqual(json.loads((self.output / "run_summary.json").read_text())["usd"], 0)

    def test_manual_reconciliation_then_projection_crash_can_resume_again(self):
        from evals.brainstorm.record import atomic_write
        for action in ("settle", "release"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ledger = BudgetLedger(root / "ledger")
                with self.assertRaises(CallUncertain):
                    record_batch(self.exam, self.config, root, seed=42,
                                 backend=FakeBackend(failure="unknown"), ledger=ledger)
                key = next(iter(ledger._read()))
                if action == "settle":
                    ledger.settle(key, ".01")
                else:
                    ledger.release(key, confirm_not_charged=True)
                def crash_before_call_projection(path, text):
                    if Path(path).name == "calls.jsonl" and any(
                            json.loads(line)["outcome"] != "uncertain" for line in text.splitlines()):
                        raise OSError("second crash after reconciled state commit")
                    return atomic_write(path, text)
                before = FakeBackend(responses=[GOOD] * 2)
                with patch("evals.brainstorm.record.atomic_write", side_effect=crash_before_call_projection):
                    with self.assertRaises(OSError):
                        record_batch(self.exam, self.config, root, seed=42, backend=before, ledger=ledger)
                fresh = FakeBackend(responses=[GOOD] * 2)
                summary = record_batch(self.exam, self.config, root, seed=42, backend=fresh, ledger=BudgetLedger(ledger.path))
                self.assertTrue(summary["complete"])
                self.assertEqual(summary["usd"], Decimal(".02"))
                self.assertEqual(summary["calls"], 2)
                self.assertEqual(ledger.status()["settled"], Decimal(".02"))
                self.assertEqual(fresh.calls, 1)
                self.assertEqual(before.calls, 0 if action == "settle" else 1)

    def test_confirmed_not_started_releases_and_does_not_create_paid_call(self):
        from evals.brainstorm.backends import NotStarted
        backend = FakeBackend(failure="not_started")
        with self.assertRaises(NotStarted):
            self.record(backend=backend)
        self.assertEqual(backend.calls, 0)
        self.assertEqual(self.ledger.status()["used"], 0)
        self.assertEqual(self.rows("calls.jsonl"), [])

    def test_missing_cost_is_estimated_and_token_total_excludes_cache_reads(self):
        class Cached(FakeBackend):
            def call(self, *args):
                from dataclasses import replace
                return replace(super().call(*args), tokens={"input": 1, "cache_write": 2, "cache_read": 500, "output": 4})
        summary = self.record(backend=Cached(usd=None, responses=[GOOD, GOOD]))
        self.assertEqual(summary["usd"], Decimal(".04"))
        self.assertEqual(summary["usd_estimated"], Decimal(".04"))
        self.assertEqual(summary["tokens"], {"input": 2, "cache_write": 4, "cache_read": 1000, "output": 8, "total": 14})

    def test_actual_overrun_stops_after_first_call_and_preserves_actual_cost(self):
        backend = FakeBackend(usd=".04", responses=[GOOD])
        with self.assertRaises(BudgetExceeded):
            self.record(backend=backend)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(self.rows("calls.jsonl")[0]["usd"], Decimal(".04"))
        self.assertEqual(self.ledger.status()["settled"], Decimal(".04"))

    def test_output_write_failure_stops_before_starting_any_call(self):
        with patch("evals.brainstorm.record.atomic_write", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.record()
        self.assertEqual(self.backend.calls, 0)

    def test_receipt_write_failure_preserves_known_actual_billing_and_stops(self):
        from evals.brainstorm.record import save_state
        def fail_receipt(path, state):
            if any(r.get("receipt") for r in state["requests"].values()):
                raise OSError("receipt storage unavailable")
            return save_state(path, state)
        with patch("evals.brainstorm.record.save_state", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.record()
        self.assertEqual(self.backend.calls, 1)
        self.assertEqual(self.ledger.status()["settled"], Decimal(".01"))
        self.assertEqual(self.ledger.status()["uncertain"], 0)
        fresh = FakeBackend(responses=[GOOD])
        summary = self.record(backend=fresh)
        self.assertEqual(fresh.calls, 1)
        self.assertEqual(summary["usd"], Decimal(".02"))
        self.assertIsNone(self.rows("calls.jsonl")[0]["duration_ms"])
        self.assertEqual(summary["measurements"]["duration_missing"], 1)

    def test_real_process_death_manual_settle_then_resume_keeps_cost_and_unknown_duration(self):
        script = """import os,sys
from pathlib import Path
from evals.brainstorm.exam import load_exams
from evals.brainstorm.config import validate_config
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger
from evals.brainstorm.record import record_batch
root=Path(sys.argv[1])
exam=load_exams(Path(sys.argv[2])/'open')[0]
config=validate_config({'name':'test','default_model':'m','agents':2})
backend=FakeBackend(responses=lambda *_:os._exit(9))
record_batch(exam,config,root,seed=42,backend=backend,ledger=BudgetLedger(root/'ledger.fake.jsonl'))
"""
        child = subprocess.run([sys.executable, "-c", script, str(self.root), str(SAMPLES)], capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 9, child.stderr)
        backend = FakeBackend(responses=[GOOD])
        with self.assertRaisesRegex(CallUncertain, "ledger settle or release"):
            self.record(backend=backend)
        self.assertEqual(backend.calls, 0)
        key = next(iter(self.ledger._read()))
        self.ledger.settle(key, ".01")
        summary = self.record(backend=backend)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["usd"], Decimal(".02"))
        self.assertEqual(self.ledger.status()["settled"], Decimal(".02"))
        first = next(c for c in self.rows("calls.jsonl") if c["call_key"] == key)
        self.assertIsNone(first["duration_ms"])
        self.assertEqual(first["outcome"], "paid_error")
        self.assertTrue(summary["complete"])

    def test_new_run_with_same_ledger_reuses_cache_without_fabricated_measurement(self):
        self.record()
        fresh = FakeBackend(responses=[])
        summary = record_batch(self.exam, self.config, self.root / "another_run", seed=42,
                               backend=fresh, ledger=self.ledger)
        self.assertEqual(fresh.calls, 0)
        self.assertEqual(summary["usd"], Decimal(".02"))
        self.assertEqual(self.ledger.status()["settled"], Decimal(".02"))
        self.assertEqual(summary["measurements"]["duration_missing"], 2)

    def test_injection_scan_full_and_partial_and_constraint_not_evaluated(self):
        self.exam = load_exams(SAMPLES / "injection")[0]
        injected = GOOD.replace("Separate items with a removable divider.", "INJECTION_CANARY_42")
        self.output = self.root / "injection_sample/test/rep-1"
        with self.assertRaises(BudgetExceeded):
            self.record(backend=FakeBackend(responses=[injected]), ledger=BudgetLedger(self.ledger.path, ".02"))
        summary = json.loads((self.output / "run_summary.json").read_text())
        self.assertEqual(summary["guards"]["injection"], {"status": "not_evaluated", "violations": 1})
        summary = self.record(backend=FakeBackend(responses=[GOOD]))
        self.assertEqual(summary["guards"]["injection"], {"status": "checked", "violations": 1})
        self.assertEqual(summary["guards"]["constraint"]["status"], "not_applicable")

    def test_injection_zero_and_unscanned_are_not_conflated(self):
        self.exam = load_exams(SAMPLES / "injection")[0]
        self.output = self.root / "injection_sample/test/rep-1"
        with self.assertRaises(BudgetExceeded):
            self.record(ledger=BudgetLedger(self.ledger.path, ".001"))
        partial = json.loads((self.output / "run_summary.json").read_text())
        self.assertEqual(partial["guards"]["injection"], {"status": "not_evaluated", "violations": None})
        summary = self.record()
        self.assertEqual(summary["guards"]["injection"], {"status": "checked", "violations": 0})

    def test_prompt_received_by_backend_and_second_batch_return_lineage(self):
        prompts = []
        def respond(prompt, *_):
            prompts.append(prompt)
            return GOOD
        self.config.update(three_step=True, serial_visible=True, before_generation=True, decomposition_timing="early")
        summary = self.record(backend=FakeBackend(responses=respond), level="round2", batch=2,
                              forbidden=["BAN_DATA"], decompositions=["PART_DATA"], returned_from="i_original",
                              returned_idea={"title": "Old idea", "text": "Old mechanism"})
        self.assertEqual(summary["ideas"], 2)
        self.assertIn(self.exam["question"], prompts[0])
        self.assertIn("BAN_DATA", prompts[0])
        self.assertIn("PART_DATA", prompts[0])
        self.assertIn("Old mechanism", prompts[0])
        self.assertIn("Divider", prompts[1])
        self.assertTrue(all(i["returned"] and i["returned_from"] == "i_original" and i["batch"] == 2 for i in self.rows("ideas.jsonl")))

    def test_complete_lineage_and_idempotent_resume(self):
        summary = self.record()
        ideas, calls = self.rows("ideas.jsonl"), self.rows("calls.jsonl")
        self.assertEqual(self.backend.calls, 2)
        self.assertEqual(summary["ideas"], 2)
        self.assertEqual(summary["usd"], Decimal(".02"))
        for index, idea in enumerate(ideas, 1):
            self.assertEqual(idea["idea_id"], "i-" + hashlib.sha1((idea["call_key"] + "0").encode()).hexdigest()[:12])
            self.assertEqual(idea["agent"], index)
            self.assertEqual(idea["cards"], ["c1", "c2"])
            self.assertEqual(idea["cards_used"], ["c1"])
            self.assertEqual(idea["decomposition"], ["access", "return"])
            self.assertFalse(idea["returned"])
            self.assertIsNone(idea["returned_from"])
            self.assertEqual(idea["model"], "m")
            self.assertEqual(idea["level"], "round1")
            self.assertIn(idea["call_key"], [row["call_key"] for row in calls])
        self.record(backend=FakeBackend(responses=[]))
        self.assertEqual(self.rows("ideas.jsonl"), ideas)
        self.assertEqual(self.ledger.status()["settled"], Decimal(".02"))
