from decimal import Decimal
import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from evals.brainstorm.budget import BudgetLedger, BudgetExceeded
from unittest.mock import patch
from evals.brainstorm.record import import_results


def external_fixture():
    idea = {"idea_id": "i-" + hashlib.sha1(b"external_call0").hexdigest()[:12], "exam_id": "external", "config": "baseline", "rep": 1,
            "agent": 1, "batch": 1, "level": "baseline", "model": "m", "method": None,
            "cards": ["c1"], "cards_used": ["c1"], "returned": False, "returned_from": None,
            "title": "External divider", "text": "Use a divider", "decomposition": None, "call_key": "external_call"}
    call = {"call_key": "external_call", "exam_id": "external", "config": "baseline", "rep": 1,
            "agent": 1, "level": "baseline", "purpose": "generate", "backend": "external",
            "model": "m", "attempt": 1, "started_at": "2026-01-01T00:00:00Z", "duration_ms": 50,
            "tokens": {"input": 1, "cache_write": 2, "cache_read": 3, "output": 4},
            "usd": .1, "usd_estimated": False, "outcome": "ok"}
    return idea, call


class ImportTests(unittest.TestCase):
    def test_committed_external_call_requires_identical_idea_set_on_reimport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            idea, call = external_fixture()
            (root / "ideas").write_text(json.dumps(idea) + "\n")
            (root / "calls").write_text(json.dumps(call) + "\n")
            ledger = BudgetLedger(root / "ledger")
            import_results(root / "ideas", root / "calls", root / "run", ledger=ledger)
            unit = root / "run/external/baseline/rep-1"
            before = {p.name: p.read_bytes() for p in unit.iterdir()}
            billed = ledger.path.read_bytes()
            extra = {**idea, "idea_id": "i-" + hashlib.sha1(b"external_call1").hexdigest()[:12], "text": "Appended mechanism"}
            for incoming in ([idea, extra], []):
                with self.subTest(incoming=len(incoming)):
                    (root / "ideas").write_text("".join(json.dumps(row) + "\n" for row in incoming))
                    with self.assertRaisesRegex(ValueError, "identical|immutable"):
                        import_results(root / "ideas", root / "calls", root / "run", ledger=ledger)
                    self.assertEqual(before, {p.name: p.read_bytes() for p in unit.iterdir()})
                    self.assertEqual(billed, ledger.path.read_bytes())

    def test_paid_error_receipt_keeps_ledger_outcome_and_imports_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, call = external_fixture()
            call["outcome"] = "paid_error"
            (root / "ideas").write_text("")
            (root / "calls").write_text(json.dumps(call) + "\n")
            ledger = BudgetLedger(root / "ledger")
            for _ in range(2):
                result = import_results(root / "ideas", root / "calls", root / "run", ledger=ledger)
                self.assertEqual(result, {"ideas": 0, "calls": 1})
            self.assertEqual(ledger.status()["settled"], Decimal(".1"))
            event = list(ledger._read().values())[0]["last"]
            self.assertEqual(event["outcome"], "paid_error")
            stored = json.loads((root / "run/external/baseline/rep-1/calls.jsonl").read_text())
            self.assertEqual(stored["outcome"], "paid_error")
            call["usd"] = .2
            (root / "calls").write_text(json.dumps(call) + "\n")
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                import_results(root / "ideas", root / "calls", root / "other_run", ledger=ledger)
            self.assertEqual(ledger.status()["settled"], Decimal(".1"))

    def test_import_rejects_another_call_or_ordinal_id_before_billing(self):
        for forged in ("i-ext", "i-000000000000", "i-" + hashlib.sha1(b"external_call1").hexdigest()[:12]):
            with self.subTest(forged=forged), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                idea, call = external_fixture()
                idea["idea_id"] = forged
                (root / "ideas").write_text(json.dumps(idea) + "\n")
                (root / "calls").write_text(json.dumps(call) + "\n")
                ledger = BudgetLedger(root / "ledger")
                with self.assertRaises(ValueError):
                    import_results(root / "ideas", root / "calls", root / "run", ledger=ledger)
                self.assertFalse(ledger.path.exists())

    def make_violating_unit(self, root, text="INJECTION_CANARY_42"):
        from evals.brainstorm.backends import FakeBackend
        from evals.brainstorm.config import validate_config
        from evals.brainstorm.exam import load_exams
        from evals.brainstorm.record import record_batch
        exam = load_exams(Path(__file__).resolve().parents[1] / "exams/samples/injection")[0]
        config = validate_config({"name": "test", "default_model": "m", "agents": 1})
        response = json.dumps({"ideas": [{"title": "Probe", "text": text, "cards_used": ["c1"], "decomposition": None}]})
        ledger = BudgetLedger(root / "ledger")
        record_batch(exam, config, root, seed=1, backend=FakeBackend(responses=[response]), ledger=ledger)
        return root / "injection_sample/test/rep-1", ledger

    def test_recorded_call_cannot_append_an_idea_under_its_existing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit, ledger = self.make_violating_unit(root, text="Clean mechanism")
            idea = json.loads((unit / "ideas.jsonl").read_text())
            extra = {**idea, "idea_id": "i-" + hashlib.sha1((idea["call_key"] + "1").encode()).hexdigest()[:12],
                     "text": "INJECTION_CANARY_42"}
            (root / "incoming").write_text(json.dumps(idea) + "\n" + json.dumps(extra) + "\n")
            before = {p.name: p.read_bytes() for p in unit.iterdir()}
            billed = ledger.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "recorded|recording"):
                import_results(root / "incoming", unit / "calls.jsonl", root, ledger=ledger)
            self.assertEqual(before, {p.name: p.read_bytes() for p in unit.iterdir()})
            self.assertEqual(billed, ledger.path.read_bytes())

    def test_recorded_key_cannot_be_relabelled_into_a_new_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit, ledger = self.make_violating_unit(root)
            idea = json.loads((unit / "ideas.jsonl").read_text())
            call = json.loads((unit / "calls.jsonl").read_text())
            for row in (idea, call):
                row["config"] = "other_config"
            (root / "incoming_ideas").write_text(json.dumps(idea) + "\n")
            (root / "incoming_calls").write_text(json.dumps(call) + "\n")
            billed = ledger.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "recorded|recording"):
                import_results(root / "incoming_ideas", root / "incoming_calls", root, ledger=ledger)
            self.assertFalse((root / "injection_sample/other_config").exists())
            self.assertEqual(billed, ledger.path.read_bytes())

    def test_self_import_is_rejected_without_losing_known_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit, ledger = self.make_violating_unit(root)
            before = {p.name: p.read_bytes() for p in unit.iterdir()}
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "recorded|recording"):
                    import_results(unit / "ideas.jsonl", unit / "calls.jsonl", root, ledger=ledger)
                summary = json.loads((unit / "run_summary.json").read_text())
                self.assertEqual(summary["guards"]["injection"], {"status": "checked", "violations": 1})
            self.assertEqual(ledger.status()["settled"], Decimal(".01"))
            self.assertEqual(before, {p.name: p.read_bytes() for p in unit.iterdir()})

    def test_external_append_to_recording_is_rejected_before_any_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit, ledger = self.make_violating_unit(root)
            idea, call = external_fixture()
            for row in (idea, call):
                row.update(exam_id="injection_sample", config="test")
            (root / "external_ideas").write_text(json.dumps(idea) + "\n")
            (root / "external_calls").write_text(json.dumps(call) + "\n")
            before = {p.name: p.read_bytes() for p in unit.iterdir()}
            for _ in range(2):
                with patch("evals.brainstorm.record.export_outputs") as export:
                    with self.assertRaisesRegex(ValueError, "recorded|recording"):
                        import_results(root / "external_ideas", root / "external_calls", root, ledger=ledger)
                    export.assert_not_called()
                summary = json.loads((unit / "run_summary.json").read_text())
                self.assertEqual(summary["guards"]["injection"], {"status": "checked", "violations": 1})
            self.assertEqual(ledger.status()["settled"], Decimal(".01"))
            self.assertEqual(before, {p.name: p.read_bytes() for p in unit.iterdir()})

    def test_serial_record_rejects_import_then_resumes_without_new_calls(self):
        from evals.brainstorm.backends import FakeBackend
        from evals.brainstorm.config import validate_config
        from evals.brainstorm.exam import load_exams
        from evals.brainstorm.record import record_batch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam = load_exams(Path(__file__).resolve().parents[1] / "exams/samples/open")[0]
            config = validate_config({"name": "test", "default_model": "m", "agents": 2, "serial_visible": True})
            response = json.dumps({"ideas": [{"title": "Own", "text": "A mechanism", "cards_used": [], "decomposition": None}]})
            ledger = BudgetLedger(root / "ledger")
            record_batch(exam, config, root, seed=1, backend=FakeBackend(responses=[response] * 2), ledger=ledger)
            idea, call = external_fixture()
            for row in (idea, call):
                row.update(exam_id="open_sample", config="test", level="round1")
            (root / "external_ideas").write_text(json.dumps(idea) + "\n")
            (root / "external_calls").write_text(json.dumps(call) + "\n")
            with self.assertRaisesRegex(ValueError, "recorded|recording"):
                import_results(root / "external_ideas", root / "external_calls", root, ledger=ledger)
            backend = FakeBackend(responses=[])
            result = record_batch(exam, config, root, seed=1, backend=backend, ledger=ledger)
            self.assertEqual(backend.calls, 0)
            self.assertEqual(result["ideas"], 2)
            self.assertEqual(ledger.status()["settled"], Decimal(".02"))

    def test_importing_already_recorded_generation_is_rejected_without_charge(self):
        from evals.brainstorm.backends import FakeBackend
        from evals.brainstorm.config import validate_config
        from evals.brainstorm.exam import load_exams
        from evals.brainstorm.record import record_batch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam = load_exams(Path(__file__).resolve().parents[1] / "exams/samples/open")[0]
            config = validate_config({"name": "test", "default_model": "m", "agents": 1})
            response = json.dumps({"ideas": [{"title": "Divider", "text": "Use a divider", "cards_used": [], "decomposition": None}]})
            ledger = BudgetLedger(root / "ledger", "1")
            record_batch(exam, config, root, seed=1, backend=FakeBackend(responses=[response]), ledger=ledger)
            unit = root / "open_sample/test/rep-1"
            with self.assertRaisesRegex(ValueError, "recorded|recording"):
                import_results(unit / "ideas.jsonl", unit / "calls.jsonl", root, ledger=ledger)
            self.assertEqual(ledger.status()["settled"], Decimal(".01"))
            self.assertEqual(len((unit / "ideas.jsonl").read_text().splitlines()), 1)

    def test_estimated_receipts_and_unknown_external_guards_stay_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            idea, call = external_fixture()
            call["usd_estimated"] = True
            (root / "ideas").write_text(json.dumps(idea) + "\n")
            (root / "calls").write_text(json.dumps(call) + "\n")
            ledger = BudgetLedger(root / "ledger", "1")
            import_results(root / "ideas", root / "calls", root / "run", ledger=ledger)
            self.assertEqual(ledger.status()["estimated"], Decimal(".1"))
            summary = json.loads((root / "run/external/baseline/rep-1/run_summary.json").read_text())
            self.assertEqual(summary["guards"]["injection"], {"status": "not_evaluated", "violations": None})

    def test_hostile_receipt_types_fail_as_validation_errors(self):
        from evals.brainstorm.record import validate_call, validate_idea
        idea, call = external_fixture()
        for field in ("purpose", "outcome", "level", "tokens", "usd", "call_key", "duration_ms"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_call({**call, field: {"unexpected": []}})
        for field in ("level", "cards", "cards_used", "returned", "decomposition", "idea_id"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_idea({**idea, field: {"unexpected": []}})

    def test_bad_input_is_validated_before_any_charge_or_idea_commit(self):
        for change in ("missing_usd", "unknown_call", "bad_idea", "duplicate_conflict", "bad_json"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                idea, call = external_fixture()
                if change == "missing_usd":
                    del call["usd"]
                if change == "unknown_call":
                    idea["call_key"] = "missing"
                if change == "bad_idea":
                    idea["returned"] = "false"
                rows = [idea, {**idea, "text": "conflict"}] if change == "duplicate_conflict" else [idea]
                (root / "ideas").write_text("{broken\n" if change == "bad_json" else "".join(json.dumps(i) + "\n" for i in rows))
                (root / "calls").write_text(json.dumps(call) + "\n")
                ledger = BudgetLedger(root / "ledger", "1")
                with self.assertRaises(ValueError):
                    import_results(root / "ideas", root / "calls", root / "run", ledger=ledger)
                self.assertFalse(ledger.path.exists())
                self.assertEqual(list((root / "run").rglob("ideas.jsonl")), [])

    def test_budget_rejection_explains_external_cost_and_zero_receipt_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            idea, call = external_fixture()
            (root / "ideas").write_text(json.dumps(idea) + "\n")
            (root / "calls").write_text(json.dumps(call) + "\n")
            ledger = BudgetLedger(root / "ledger", ".01")
            with self.assertRaisesRegex(BudgetExceeded, "External costs already incurred.*not imported"):
                import_results(root / "ideas", root / "calls", root / "run", ledger=ledger)
            self.assertEqual(ledger.status()["used"], 0)
            ledger.limit = Decimal("1")
            import_results(root / "ideas", root / "calls", root / "run", ledger=ledger)
            self.assertEqual(ledger.status()["settled"], Decimal(".1"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            idea, call = external_fixture()
            call["usd"] = 0
            (root / "ideas").write_text(json.dumps(idea) + "\n")
            (root / "calls").write_text(json.dumps(call) + "\n")
            ledger = BudgetLedger(root / "ledger", "1")
            import_results(root / "ideas", root / "calls", root / "run", ledger=ledger)
            self.assertEqual(ledger.status()["settled"], 0)

    def test_paid_receipt_recovers_output_gap_and_changed_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            idea, call = external_fixture()
            (root / "ideas").write_text(json.dumps(idea) + "\n")
            (root / "calls").write_text(json.dumps(call) + "\n")
            ledger = BudgetLedger(root / "ledger", "1")
            with patch("evals.brainstorm.record.export_outputs", side_effect=OSError("crash")):
                with self.assertRaises(OSError):
                    import_results(root / "ideas", root / "calls", root / "run", ledger=ledger)
            import_results(root / "ideas", root / "calls", root / "run", ledger=ledger)
            self.assertEqual(ledger.status()["settled"], Decimal(".1"))
            call["usd"] = .2
            (root / "calls").write_text(json.dumps(call) + "\n")
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                import_results(root / "ideas", root / "calls", root / "run", ledger=ledger)
            self.assertEqual(ledger.status()["settled"], Decimal(".1"))
    def test_import_preserves_lineage_and_bills_receipt_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            idea, call = external_fixture()
            (root / "ideas.jsonl").write_text(json.dumps(idea) + "\n")
            (root / "calls.jsonl").write_text(json.dumps(call) + "\n")
            ledger = BudgetLedger(root / "ledger.fake.jsonl", "1")
            for _ in range(2):
                import_results(root / "ideas.jsonl", root / "calls.jsonl", root / "runs", ledger=ledger)
            output = root / "runs/external/baseline/rep-1"
            self.assertEqual(json.loads((output / "ideas.jsonl").read_text()), idea)
            self.assertEqual(ledger.status()["settled"], Decimal(".1"))
            self.assertEqual(len((output / "calls.jsonl").read_text().splitlines()), 1)
