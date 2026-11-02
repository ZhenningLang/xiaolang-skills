"""Fake recording -> real CLI -> incremental partition -> isolated calibration."""

from contextlib import redirect_stdout
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals.brainstorm import dedupe
from evals.brainstorm.__main__ import main
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger, BudgetExceeded
from evals.brainstorm.budget import dumps
from evals.brainstorm.config import validate_config
from evals.brainstorm.exam import load_exams
from evals.brainstorm.record import record_batch, read_rows
from evals.brainstorm.tests.test_dedupe import group, idea, response


class DedupeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ledger = BudgetLedger(self.root / "ledger.fake.jsonl")
        self.rows = [idea(i) for i in range(4)]
        self.ids = [row["idea_id"] for row in self.rows]
        for target in ("evals.brainstorm.backends.ClaudeCliBackend.call",
                       "evals.brainstorm.backends.CommandBackend.call", "socket.create_connection",
                       "socket.socket.connect"):
            self.enterContext(patch(target, side_effect=AssertionError("Real backends/network forbidden")))

    def human(self, groups=None, **changes):
        return {"exam_id": "sample", "groups": groups if groups is not None else [self.ids[:2], self.ids[2:]], **changes}

    def calibrate(self, human=None, backend=None, **options):
        return dedupe.calibrate(self.rows, self.human() if human is None else human, self.root,
                                exam_id="sample", seed=42, model_a="a", model_b="b", allowed_models=["a", "b"],
                                backend=backend or FakeBackend(responses=[response(group([self.ids[0], self.ids[2]]),
                                    group([self.ids[1]], "new:two"), group([self.ids[3]], "new:three")),
                                    response(group(self.ids[:2]), group(self.ids[2:], "new:two"))]),
                                ledger=self.ledger, name="calibration", **options)

    def test_calibration_reports_actual_false_merges_and_splits(self):
        result = self.calibrate()
        self.assertFalse(result["authoritative"])
        self.assertEqual(result["status"], "complete")
        self.assertAlmostEqual(result["ami"]["a_human"], -.2857142857142857, places=10)
        self.assertEqual(result["ami"]["b_human"], 1)
        self.assertAlmostEqual(result["ami"]["a_b"], result["ami"]["a_human"], places=10)
        self.assertEqual([row["idea_ids"] for row in result["differences"]["a"]["false_merges"]], [[self.ids[0], self.ids[2]]])
        self.assertEqual([row["idea_ids"] for row in result["differences"]["a"]["false_splits"]], [self.ids[:2], self.ids[2:]])
        self.assertEqual(result["differences"]["b"], {"false_merges": [], "false_splits": []})
        calls = [row for name in ("a", "b") for row in read_rows(result["outputs"][name]["calls_path"])]
        self.assertEqual({row["model"] for row in calls}, {"a", "b"})
        self.assertEqual(sum(row["usd"] for row in calls), Decimal(".02"))
        before = self.ledger.path.read_bytes()
        self.calibrate(backend=FakeBackend(responses=[]))
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertEqual(result["groups"]["human"], self.human()["groups"])
        self.assertEqual(result["difference_counts"]["a"], {"false_merges": 1, "false_splits": 2})
        self.assertEqual({row["idea_id"]: row["title"] for row in result["ideas"]},
                         {row["idea_id"]: row["title"] for row in self.rows})
        pair = result["differences"]["a"]["false_merges"][0]
        self.assertEqual(pair["machine_groups"], [0, 0])
        for key, machine_index, human_index in zip(pair["idea_ids"], pair["machine_groups"], pair["human_groups"]):
            self.assertIn(key, result["groups"]["a"][machine_index])
            self.assertIn(key, result["groups"]["human"][human_index])

    def test_difference_payload_keeps_all_pairs_without_cubic_group_copies(self):
        ids = [f"item_{i}" for i in range(80)]
        result = dedupe.differences([[key] for key in ids], [ids], ids)
        self.assertEqual(len(result["false_splits"]), 3160)
        self.assertLess(len(dumps(result)), 600000)
        self.assertTrue(all(type(index) is int for pair in result["false_splits"] for index in pair["human_groups"]))

    def test_empty_calibration_is_not_reported_as_perfect_quality(self):
        with self.assertRaisesRegex(ValueError, "nonempty"):
            dedupe.calibrate([], {"exam_id": "sample", "groups": []}, self.root, exam_id="sample", seed=1,
                             model_a="a", model_b="b", allowed_models=["a", "b"], backend=FakeBackend(),
                             ledger=self.ledger, name="empty")
        self.assertFalse((self.root / "replay").exists())
        self.assertFalse(self.ledger.path.exists())

    def test_bad_human_partitions_fail_before_cost_or_replay_outputs(self):
        for human in (self.human(groups=[]), self.human(groups=[self.ids[:2]]),
                      self.human(groups=[self.ids, [self.ids[0]]]), self.human(groups=[self.ids + ["x"]]),
                      self.human(exam_id="other")):
            with self.subTest(human=human), self.assertRaises(ValueError):
                self.calibrate(human=human)
        self.assertFalse(self.ledger.path.exists())
        self.assertFalse((self.root / "replay").exists())

    def test_requires_explicit_distinct_allowed_models(self):
        for a, b, allowed in (("a", None, ["a", "b"]), ("a", "a", ["a"]), ("a", "b", ["a"])):
            with self.subTest(a=a, b=b), self.assertRaises(ValueError):
                dedupe.calibrate(self.rows, self.human(), self.root, exam_id="sample", seed=42,
                                 model_a=a, model_b=b, allowed_models=allowed,
                                 backend=FakeBackend(), ledger=self.ledger, name="bad")
        self.assertFalse(self.ledger.path.exists())

    def test_fallbacks_are_explicit_not_a_stability_conclusion(self):
        result = self.calibrate(backend=FakeBackend(responses=["broken", response(group(self.ids))]))
        self.assertEqual(result["status"], "complete_with_fallback")
        self.assertEqual(result["fallback_counts"], {"a": 4, "b": 0})
        self.assertTrue(result["warnings"])

    def test_budget_stops_calibration_with_partial_report(self):
        self.ledger = BudgetLedger(self.ledger.path, ".02")
        with self.assertRaises(BudgetExceeded):
            self.calibrate()
        report = json.loads((self.root / "replay/calibration/calibration.json").read_text())
        self.assertEqual(report["status"], "incomplete")
        self.assertIsNone(report["ami"]["a_b"])
        self.assertIn("BudgetExceeded", report["error"])

    def test_replay_alias_cannot_write_authority(self):
        dedupe.deduplicate(self.rows, self.root, exam_id="sample", seed=42, model="a", ledger=self.ledger,
                           backend=FakeBackend(responses=[response(group(self.ids))]))
        before = (self.root / "sample/mechanisms.json").read_bytes()
        replay = self.root / "replay"
        replay.mkdir()
        (replay / "alias").symlink_to(self.root, target_is_directory=True)
        for name in ("alias", "../sample", "/sample"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                dedupe.deduplicate(self.rows, self.root, exam_id="sample", seed=42, model="a", ledger=self.ledger,
                                   backend=FakeBackend(responses=[]), replay=name)
        self.assertEqual((self.root / "sample/mechanisms.json").read_bytes(), before)

    def cli(self, *arguments):
        with patch("sys.argv", ["brainstorm", *map(str, arguments)]), redirect_stdout(io.StringIO()) as output:
            code = main()
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def test_recorded_ideas_cli_increment_replay_calibrate_without_mutating_recordings(self):
        samples = Path(__file__).resolve().parents[1] / "exams/samples/open"
        exam = load_exams(samples)[0]
        config = validate_config({"name": "recorded", "default_model": "a", "models": ["a", "b"], "agents": 1})
        generated = json.dumps({"ideas": [{"title": f"Variant {i}", "text": f"Different phrasing {i}",
                                           "cards_used": [], "decomposition": None} for i in range(4)]})
        record_batch(exam, config, self.root, seed=42, backend=FakeBackend(responses=[generated]), ledger=self.ledger)
        unit = self.root / f"{exam['id']}/recorded/rep-1"
        rows = read_rows(unit / "ideas.jsonl")
        ids = [row["idea_id"] for row in rows]
        originals = {path: path.read_bytes() for path in unit.iterdir() if path.is_file()}
        config_path = self.root / "config.json"
        input_path = self.root / "input.jsonl"
        def paid_total():
            return sum(value["last"]["usd"] for value in self.ledger._read().values())
        def protected_tree():
            excluded = {"replay", "ledger.fake.jsonl", "ledger.fake.jsonl.cache", "ledger.fake.jsonl.lock"}
            return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*")
                    if path.is_file() and path.relative_to(self.root).parts[0] not in excluded}
        def configure(payloads):
            config_path.write_text(json.dumps({**config, "backend": {"kind": "fake", "responses": payloads}}))
        def write_input(items):
            input_path.write_text("".join(json.dumps(row) + "\n" for row in items))
        common = ["--ideas", input_path, "--exam-id", exam["id"], "--run-dir", self.root,
                  "--config", config_path, "--seed", "42", "--ledger", self.ledger.path]
        configure([response(group(ids[:2]))])
        write_input(rows[:2])
        first = self.cli("dedupe", "run", *common)
        self.assertEqual(paid_total(), Decimal(".02"))
        configure([response(group([ids[2]], "m-001"), group([ids[3]], "new:two"))])
        write_input(rows)
        full = self.cli("dedupe", "run", *common)
        self.assertEqual(paid_total(), Decimal(".03"))
        disk = json.loads(Path(full["mechanisms_path"]).read_text())
        self.assertEqual(disk, full["table"])
        self.assertCountEqual([key for row in disk["mechanisms"] for key in row["idea_ids"]], ids)
        self.assertTrue(all(row["representative"] in row["idea_ids"] for row in disk["mechanisms"]))
        self.assertEqual(len(full["table"]["mechanisms"]), 2)
        self.assertEqual(full["table"]["mechanisms"][0]["representative"], first["table"]["mechanisms"][0]["representative"])
        self.assertCountEqual([key for row in full["table"]["mechanisms"] for key in row["idea_ids"]], ids)
        # Every variant remains recoverable from the immutable recording, including different text.
        by_id = {row["idea_id"]: row for row in rows}
        self.assertEqual(len({by_id[key]["text"] for key in full["table"]["mechanisms"][0]["idea_ids"]}), 3)
        before_ledger = self.ledger.path.read_bytes()
        self.cli("dedupe", "run", *common)
        self.assertEqual(self.ledger.path.read_bytes(), before_ledger)
        authority_path = Path(full["mechanisms_path"])
        authority = authority_path.read_bytes()
        configure([response(group(ids))])
        before_replay = protected_tree()
        self.enterContext(patch("evals.brainstorm.record.record_batch", side_effect=AssertionError("Replay must not generate")))
        replay = self.cli("dedupe", "run", *common, "--replay", "trial")
        self.assertFalse(replay["authoritative"])
        self.assertEqual(len(replay["table"]["mechanisms"]), 1)
        self.assertEqual(protected_tree(), before_replay)
        self.assertEqual(paid_total(), Decimal(".04"))
        human_path = self.root / "human_groups.json"
        human_path.write_text(json.dumps({"exam_id": exam["id"], "groups": [ids[:2], ids[2:]]}))
        configure([response(group([ids[0], ids[2]]), group([ids[1]], "new:two"), group([ids[3]], "new:three")),
                   response(group(ids[:2]), group(ids[2:], "new:two"))])
        before_calibration = protected_tree()
        report = self.cli("dedupe", "calibrate", *common, "--human-groups", human_path,
                          "--model-a", "a", "--model-b", "b", "--name", "cli_calibration")
        self.assertAlmostEqual(report["ami"]["a_human"], -.2857142857142857, places=10)
        self.assertEqual(report["ami"]["b_human"], 1)
        self.assertAlmostEqual(report["ami"]["a_b"], -.2857142857142857, places=10)
        self.assertEqual([pair["idea_ids"] for pair in report["differences"]["a"]["false_merges"]], [[ids[0], ids[2]]])
        self.assertEqual([pair["idea_ids"] for pair in report["differences"]["a"]["false_splits"]], [ids[:2], ids[2:]])
        self.assertEqual(json.loads((self.root / "replay/cli_calibration/calibration.json").read_text()), report)
        self.assertEqual(protected_tree(), before_calibration)
        self.assertEqual(paid_total(), Decimal(".06"))
        replay_calls = [*read_rows(replay["calls_path"]),
                        *(call for paths in report["outputs"].values() for call in read_rows(paths["calls_path"]))]
        self.assertEqual(len(replay_calls), 3)
        self.assertTrue(all(call["purpose"] == "dedupe" for call in replay_calls))
        self.assertEqual(authority_path.read_bytes(), authority)
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_cli_budget_error_is_exit_three_without_provider_call(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"name": "test", "default_model": "a"}))
        inputs = self.root / "ideas.jsonl"
        inputs.write_text("".join(json.dumps(row) + "\n" for row in self.rows))
        with patch("sys.argv", ["brainstorm", "dedupe", "run", "--ideas", str(inputs), "--exam-id", "sample",
                                "--config", str(config), "--run-dir", str(self.root), "--seed", "1", "--limit", ".001"]), \
                patch("sys.stderr", new_callable=io.StringIO), patch.object(FakeBackend, "call", side_effect=AssertionError("Must not call")):
            self.assertEqual(main(), 3)

    def test_default_fake_cli_uses_valid_merge_response_not_fallback(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"name": "test", "default_model": "a"}))
        inputs = self.root / "ideas.jsonl"
        rows = [idea(1, text="Identical mechanism"), idea(2, text="Identical mechanism")]
        inputs.write_text("".join(json.dumps(row) + "\n" for row in rows))
        result = self.cli("dedupe", "run", "--ideas", inputs, "--exam-id", "sample", "--config", config,
                          "--run-dir", self.root, "--seed", "1", "--batch-size", "1")
        self.assertEqual(len(result["table"]["mechanisms"]), 1)
        self.assertEqual(result["fallback_ids"], [])
        self.assertEqual(result["failures"], [])

    def test_calibration_rejects_prepopulated_replay_instead_of_warm_start(self):
        dedupe.deduplicate(self.rows, self.root, exam_id="sample", seed=99, model="a", ledger=self.ledger,
                           backend=FakeBackend(responses=[response(group(self.ids))]), replay="calibration__a")
        before = self.ledger.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "cold"):
            self.calibrate()
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_cli_explicit_zero_token_limit_is_rejected_not_defaulted(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"name": "test", "default_model": "a"}))
        inputs = self.root / "ideas.jsonl"
        inputs.write_text("".join(json.dumps(row) + "\n" for row in self.rows))
        with patch("sys.argv", ["brainstorm", "dedupe", "run", "--ideas", str(inputs), "--exam-id", "sample",
                                "--config", str(config), "--run-dir", str(self.root), "--seed", "1", "--max-output-tokens", "0"]), \
                patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(), 1)
        self.assertFalse(self.ledger.path.exists())

    def test_cli_paid_failure_requires_explicit_retry_and_preserves_both_charges(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"name": "test", "default_model": "a", "backend": {"kind": "fake", "failure": "paid_error"}}))
        inputs = self.root / "ideas.jsonl"
        inputs.write_text("".join(json.dumps(row) + "\n" for row in self.rows))
        arguments = ["dedupe", "run", "--ideas", str(inputs), "--exam-id", "sample", "--config", str(config),
                     "--run-dir", str(self.root), "--seed", "1", "--ledger", str(self.ledger.path)]
        with patch("sys.argv", ["brainstorm", *arguments]), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(), 1)
        config.write_text(json.dumps({"name": "test", "default_model": "a"}))
        with patch("sys.argv", ["brainstorm", *arguments]), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(), 1)
        result = self.cli(*arguments, "--retry-paid-error")
        self.assertCountEqual([key for row in result["table"]["mechanisms"] for key in row["idea_ids"]], self.ids)
        calls = read_rows(result["calls_path"])
        self.assertEqual([row["attempt"] for row in calls], [1, 2])
        self.assertEqual(sum(row["usd"] for row in calls), Decimal(".02"))
