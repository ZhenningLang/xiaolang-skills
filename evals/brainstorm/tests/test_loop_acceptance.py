"""Disk, budget, subprocess and recovery acceptance for the loop CLI/API."""

from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evals.brainstorm import loop
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger, BudgetExceeded, CallUncertain
from evals.brainstorm.config import validate_config
from evals.brainstorm.dedupe import fake_partition
from evals.brainstorm.exam import load_exams
from evals.brainstorm.record import read_file, read_rows, record_batch
from evals.brainstorm.tests.test_loop import data_after, response


def returned(prompt, text, **options):
    """A bundled collision return answers once per original, in order."""
    count = len(data_after(prompt, "Return exactly one replacement idea for each original, in the same order."))
    return response(*[text] * count, **options)


SAMPLES = Path(__file__).resolve().parents[1] / "exams/samples"


class LoopAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.classifier = self.root / "classifier.json"
        self.config.write_text(json.dumps({"name": "cli", "default_model": "generator", "agents": 1,
            "levels": ["round1", "round2", "pressure1"], "return_collisions": True,
            "backend": {"kind": "fake", "responses": [response("base")] * 4 +
                        [response("replacement"), response("second"), response("third")]}}))
        self.classifier.write_text(json.dumps({"name": "classifier", "default_model": "fixed"}))
        self.addCleanup(patch.stopall)
        for target in ("evals.brainstorm.backends.ClaudeCliBackend.call",
                       "evals.brainstorm.backends.CommandBackend.call", "socket.socket.connect"):
            patch(target, side_effect=AssertionError("No real provider or network allowed")).start()
        guard = self.root / "subprocess_guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(
            "import os\nfrom pathlib import Path\nimport socket\n"
            "from evals.brainstorm.backends import ClaudeCliBackend, CommandBackend\n"
            "log = Path(os.environ['LOOP_TEST_GUARD_LOG'])\n"
            "def note(text):\n    with log.open('a') as stream: stream.write(text + '\\n')\n"
            "def denied(*args, **kwargs):\n    note('blocked')\n    raise AssertionError('LOOP_TEST_EXTERNAL_CALL_BLOCKED')\n"
            "ClaudeCliBackend.call = denied\nCommandBackend.call = denied\nsocket.socket.connect = denied\nnote('active')\n")
        self.guard_log = self.root / "guard.log"
        patch.dict(os.environ, {"PYTHONPATH": os.pathsep.join([str(guard), str(Path(__file__).resolve().parents[3])]),
                                "LOOP_TEST_GUARD_LOG": str(self.guard_log)}).start()

    def api(self, root, *, backend=None, grouping=None, limit="50", config=None, classifier=None, seed=19, exam=None, rep=1):
        config = config or validate_config({"name": "unit", "default_model": "generator", "agents": 1,
                                           "return_collisions": True})
        classifier = classifier or validate_config({"name": "fixed", "default_model": "classifier"})
        def generated(prompt, *_):
            if "Collision return:" in prompt:
                return returned(prompt, "replacement")
            if "Generation level: baseline." in prompt:
                return response("base")
            return response("base", "base")
        return loop.run_loop(exam or load_exams(SAMPLES / "open")[0], config, root,
            seed=seed, backend=backend or FakeBackend(responses=generated), ledger=BudgetLedger(root / "ledger", limit),
            dedupe_config=classifier, dedupe_backend=grouping or FakeBackend(responses=fake_partition), rep=rep)

    def snapshot(self, root):
        unit = root / "open_sample/unit/rep-1"
        events = [e for e in read_rows(root / "ledger") if e["event"] == "settle"]
        return {"ideas": read_rows(unit / "ideas.jsonl"), "batches": read_rows(unit / "loop_batches.jsonl"),
                "table": read_file(root / "open_sample/mechanisms.json"),
                "calls": len(events), "usd": sum(e["usd"] for e in events)}

    def command(self, run="run", *extra):
        return [sys.executable, "-m", "evals.brainstorm", "loop", "--exam-dir", str(SAMPLES / "open"),
                "--config", str(self.config), "--dedupe-config", str(self.classifier), "--seed", "19",
                "--run-dir", str(self.root / run), *extra]

    def test_cli_complete_slice_and_free_rerun(self):
        before = {p: p.read_bytes() for p in (SAMPLES / "open").iterdir() if p.is_file()}
        result = subprocess.run(self.command(), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = self.root / "run/open_sample/cli/rep-1"
        ideas = read_rows(unit / "ideas.jsonl")
        rows = read_rows(unit / "loop_batches.jsonl")
        self.assertEqual([r["level"] for r in rows], ["baseline"] * 3 + ["round1", "round2", "pressure1"])
        self.assertEqual(len(ideas), 7)
        self.assertEqual(sum(i["returned"] for i in ideas), 1)
        table = read_file(self.root / "run/open_sample/mechanisms.json")["mechanisms"]
        self.assertEqual(sorted(k for m in table for k in m["idea_ids"]), sorted(i["idea_id"] for i in ideas))
        events = read_rows(self.root / "run/ledger.fake.jsonl")
        settles = [e for e in events if e["event"] == "settle"]
        self.assertEqual(len(settles), 14)
        self.assertEqual(sum(e["usd"] for e in settles), Decimal(".14"))
        repeat = subprocess.run(self.command(), capture_output=True, text=True, timeout=30)
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(read_rows(self.root / "run/ledger.fake.jsonl"), events)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertIn("stage=done", result.stderr)
        self.assertEqual(self.guard_log.read_text().splitlines(), ["active", "active"])

    def test_budget_each_stage_stops_before_call_and_resume_matches_uninterrupted(self):
        reference = self.root / "reference"
        self.api(reference)
        expected = self.snapshot(reference)
        # Before baseline generation, its grouping, ordinary generation, the bundled rewrite and its one grouping call.
        for limit, want_calls, want_stage in ((".01", 0, "generate"), (".02", 1, "dedupe"),
                                              (".07", 6, "generate"), (".08", 7, "dedupe"),
                                              (".09", 8, "rewrite"), (".10", 9, "rewrite")):
            with self.subTest(limit=limit):
                root = self.root / ("budget_" + limit)
                with self.assertRaises(BudgetExceeded):
                    self.api(root, limit=limit)
                settled = [e for e in read_rows(root / "ledger") if e["event"] == "settle"]
                self.assertEqual(len(settled), want_calls)
                state = read_file(root / "open_sample/unit/rep-1/loop_state.json")["state"]
                self.assertEqual(state["batches"][-1]["stage"], want_stage)
                self.assertIsNone(read_rows(root / "open_sample/unit/rep-1/loop_batches.jsonl")[-1]["new_mechanisms"])
                self.api(root, limit="50")
                self.assertEqual(self.snapshot(root), expected)
                self.api(root)
                self.assertEqual(self.snapshot(root), expected)

    def test_crash_windows_preserve_frozen_growth_lineage_and_charges(self):
        reference = self.root / "reference"
        self.api(reference)
        expected = self.snapshot(reference)
        class Crash(BaseException):
            pass
        for window in ("generated", "grouped", "partial_return", "finalized"):
            with self.subTest(window=window):
                root = self.root / window
                save = loop.save_state
                crashed = []
                def interrupt(path, state):
                    if state["batches"]:
                        batch = state["batches"][-1]
                        hit = batch["level"] == "round1" and (
                            window == "generated" and batch["stage"] == "dedupe" or
                            window == "grouped" and batch["stage"] == "rewrite" or
                            window == "partial_return" and batch["rewrites"] and batch["rewrites"][0]["stage"] == "done" or
                            window == "finalized" and batch["stage"] == "done")
                        if hit and not crashed:
                            crashed.append(True)
                            raise Crash()
                    save(path, state)
                with patch.object(loop, "save_state", side_effect=interrupt), self.assertRaises(Crash):
                    self.api(root)
                self.api(root)
                self.assertEqual(self.snapshot(root), expected)

    def test_cross_configuration_classifier_and_local_growth_are_order_independent(self):
        for order in (("a", "b"), ("b", "a")):
            root = self.root / "".join(order)
            for index, name in enumerate(order):
                config = validate_config({"name": name, "default_model": "model_" + name, "agents": 1})
                self.api(root, config=config, backend=FakeBackend(responses=[response("base")] * 3 + [response("shared")]))
                row = read_rows(root / f"open_sample/{name}/rep-1/loop_batches.jsonl")[-1]
                self.assertEqual(row["new_mechanisms"], 1)
                self.assertEqual(len(row["authority_new_mechanism_ids"]), 1 if index == 0 else 0)
            classifier = read_file(root / "open_sample/dedupe/state.json")["state"]["classifier"]
            self.assertEqual(classifier, {"backend": "fake", "model": "classifier"})

    def test_config_order_preserves_prompts_exhaustion_returns_and_call_counts(self):
        observed = {}
        for order in (("a", "b"), ("b", "a")):
            root = self.root / "".join(order)
            for name in order:
                prompts = []
                def generated(prompt, *_):
                    prompts.append(prompt)
                    level = prompt.split("Generation level: ", 1)[1].split(".", 1)[0]
                    text = {"baseline": name + "_BASE", "round1": "COMMON" if name == "a" else "a_BASE",
                            "round2": "A_ONLY" if name == "a" else "COMMON", "pressure3": name + "_LAST"}[level]
                    return response(text, decomposition=[name + "_DIM"])
                config = validate_config({"name": name, "default_model": "model_" + name, "agents": 1,
                    "return_collisions": True, "max_batches": 2, "levels": ["round1", "round2", "pressure3"]})
                backend, grouping = FakeBackend(responses=generated), FakeBackend(responses=fake_partition)
                result = self.api(root, config=config, backend=backend, grouping=grouping)
                unit = root / f"open_sample/{name}/rep-1"
                batches = read_rows(unit / "loop_batches.jsonl")
                facts = {"prompts": prompts, "growth": [(r["level"], r["new_mechanisms"]) for r in batches],
                         "returned": sum(i["returned"] for i in read_rows(unit / "ideas.jsonl")),
                         "generation_calls": backend.calls, "group_calls": grouping.calls,
                         "stop_reason": result["stop_reason"]}
                self.assertEqual(facts["growth"], [("baseline", 0)] * 3 + [("round1", 1), ("round1", 0),
                                 ("round2", 1), ("round2", 0), ("pressure3", 1), ("pressure3", 0)])
                self.assertEqual((facts["returned"], backend.calls, grouping.calls), (0, 9, 9))
                if name in observed:
                    self.assertEqual(facts, observed[name])
                else:
                    observed[name] = facts

    def test_real_rewrite_prompts_match_isolated_and_both_config_orders(self):
        expected = None
        for order in (("b",), ("a", "b"), ("b", "a")):
            root = self.root / ("rewrite_" + "".join(order))
            for name in order:
                prompts = []
                def generated(prompt, *_):
                    prompts.append(prompt)
                    if "Collision return:" in prompt:
                        return returned(prompt, name + "_REPLACEMENT", decomposition=[name + "_DIM"])
                    level = prompt.split("Generation level: ", 1)[1].split(".", 1)[0]
                    text = {"baseline": name + "_BASE", "round1": "A_ONLY" if name == "a" else "b_BASE",
                            "pressure3": name + "_LAST"}[level]
                    return response(text, decomposition=[name + "_DIM"])
                config = validate_config({"name": name, "default_model": name, "agents": 1,
                                          "return_collisions": True, "levels": ["round1", "pressure3"]})
                backend, grouping = FakeBackend(responses=generated), FakeBackend(responses=fake_partition)
                self.api(root, config=config, backend=backend, grouping=grouping)
                if name != "b":
                    continue
                rewrites = [p for p in prompts if "Collision return:" in p]
                self.assertEqual(len(rewrites), 1)
                self.assertTrue(all("A_ONLY" not in p and "a_BASE" not in p and "a_DIM" not in p for p in prompts))
                unit = root / "open_sample/b/rep-1"
                ideas = read_rows(unit / "ideas.jsonl")
                children = [i for i in ideas if i["returned"]]
                self.assertEqual(len(children), 1)
                parent = next(i for i in ideas if i["idea_id"] == children[0]["returned_from"])
                self.assertEqual((parent["text"], children[0]["text"]), ("b_BASE", "b_REPLACEMENT"))
                facts = {"prompts": prompts, "rewrite_prompts": rewrites,
                         "growth": [r["new_mechanisms"] for r in read_rows(unit / "loop_batches.jsonl")],
                         "generation_calls": backend.calls, "group_calls": grouping.calls}
                self.assertEqual((backend.calls, grouping.calls), (6, 6))
                self.assertEqual(facts["growth"], [0, 0, 0, 1, 1])
                if expected is None:
                    expected = facts
                else:
                    self.assertEqual(facts, expected)

    def test_second_rep_collision_reference_excludes_first_rep_baseline(self):
        root = self.root / "rep_reference"
        config = validate_config({"name": "unit", "default_model": "generator", "agents": 1,
                                  "return_collisions": True})
        self.api(root, config=config, backend=FakeBackend(responses=[response("REP1_BASE")] * 3 + [response("REP1_FRESH")]))
        def generated(prompt, *_):
            if "Collision return:" in prompt:
                return response("UNEXPECTED_REPLACEMENT")
            return response("REP2_BASE" if "Generation level: baseline." in prompt else "REP1_BASE")
        backend = FakeBackend(responses=generated)
        self.api(root, config=config, rep=2, backend=backend)
        unit = root / "open_sample/unit/rep-2"
        ideas = read_rows(unit / "ideas.jsonl")
        self.assertEqual([i["text"] for i in ideas if i["level"] == "round1"], ["REP1_BASE"])
        self.assertEqual(sum(i["returned"] for i in ideas), 0)
        self.assertEqual(backend.calls, 4)
        self.assertEqual(read_rows(unit / "loop_batches.jsonl")[-1]["returned_idea_ids"], [])

    def test_classifier_labels_never_enter_later_or_rewrite_prompts(self):
        root = self.root / "independent_label"
        config = validate_config({"name": "unit", "default_model": "generator", "agents": 1,
                                  "return_collisions": True, "levels": ["round1", "pressure1"]})
        prompts = []
        def generated(prompt, *_):
            prompts.append(prompt)
            if "Collision return:" in prompt:
                return returned(prompt, "replacement")
            return response("last" if "Generation level: pressure1." in prompt else "base")
        def classified(prompt, *args):
            partition = json.loads(fake_partition(prompt, *args))
            for group in partition["groups"]:
                group["label"] = "CLASSIFIER_ONLY_LABEL"
            return json.dumps(partition)
        self.api(root, config=config, backend=FakeBackend(responses=generated), grouping=FakeBackend(responses=classified))
        table = read_file(root / "open_sample/mechanisms.json")["mechanisms"]
        self.assertEqual({row["label"] for row in table}, {"CLASSIFIER_ONLY_LABEL"})
        self.assertEqual(len(table), 3)
        protected = [p for p in prompts if "Collision return:" in p or "Generation level: pressure1." in p]
        self.assertEqual(len(protected), 2)
        self.assertTrue(all("CLASSIFIER_ONLY_LABEL" not in p for p in protected))
        self.assertTrue(all("base" in p for p in protected))

    def test_interleaved_authority_growth_excludes_other_units_new_mechanisms(self):
        root = self.root / "interleaved"
        a = validate_config({"name": "a", "default_model": "a", "agents": 1})
        b = validate_config({"name": "b", "default_model": "b", "agents": 1})
        def generated_a(prompt, *_):
            return response("A_BASE" if "Generation level: baseline." in prompt else "A_CURRENT")
        first = FakeBackend(responses=generated_a)
        with self.assertRaises(BudgetExceeded):
            self.api(root, config=a, backend=first, limit=".07")
        unit = root / "open_sample/a/rep-1"
        pending = read_file(unit / "loop_state.json")["state"]["batches"][-1]
        self.assertEqual((pending["level"], pending["stage"], first.calls), ("round1", "generate", 3))
        self.assertEqual(pending["authority_before"], ["m-001"])
        self.api(root, config=b, backend=FakeBackend(responses=[response("B_BASE")] * 3 + [response("B_CURRENT")]))
        table_before = read_file(root / "open_sample/mechanisms.json")["mechanisms"]
        self.assertEqual([row["label"] for row in table_before], ["A_BASE", "B_BASE", "B_CURRENT"])
        resumed = FakeBackend(responses=generated_a)
        self.api(root, config=a, backend=resumed)
        batch = read_rows(unit / "loop_batches.jsonl")[-1]
        self.assertEqual(resumed.calls, 1)
        self.assertEqual(batch["new_mechanisms"], 1)
        self.assertEqual(batch["authority_new_mechanism_ids"], ["m-004"])
        current = next(i for i in read_rows(unit / "ideas.jsonl") if i["text"] == "A_CURRENT")
        final = read_file(root / "open_sample/mechanisms.json")["mechanisms"]
        self.assertEqual(final[-1]["mechanism_id"], "m-004")
        self.assertEqual(final[-1]["idea_ids"], [current["idea_id"]])
        settled = [e for e in read_rows(root / "ledger") if e["event"] == "settle"]
        self.assertEqual((len(settled), sum(e["usd"] for e in settled)), (16, Decimal(".16")))

    def test_interleaved_join_is_unit_new_but_only_creator_claims_authority_new(self):
        root = self.root / "interleaved_join"
        a = validate_config({"name": "a", "default_model": "a", "agents": 1})
        b = validate_config({"name": "b", "default_model": "b", "agents": 1})
        def generated_a(prompt, *_):
            return response("A_BASE" if "Generation level: baseline." in prompt else "SHARED")
        with self.assertRaises(BudgetExceeded):
            self.api(root, config=a, backend=FakeBackend(responses=generated_a), limit=".07")
        self.api(root, config=b, backend=FakeBackend(responses=[response("B_BASE")] * 3 + [response("SHARED")]))
        self.api(root, config=a, backend=FakeBackend(responses=generated_a))
        a_rows = read_rows(root / "open_sample/a/rep-1/loop_batches.jsonl")
        b_rows = read_rows(root / "open_sample/b/rep-1/loop_batches.jsonl")
        self.assertEqual((a_rows[-1]["new_mechanisms"], b_rows[-1]["new_mechanisms"]), (1, 1))
        self.assertEqual(a_rows[-1]["authority_new_mechanism_ids"], [])
        self.assertEqual(b_rows[-1]["authority_new_mechanism_ids"], ["m-003"])
        table = read_file(root / "open_sample/mechanisms.json")["mechanisms"]
        claims = [key for row in a_rows + b_rows for key in row["authority_new_mechanism_ids"]]
        self.assertEqual(sorted(claims), ["m-001", "m-002", "m-003"])
        self.assertEqual(len(table), 3)
        b_shared = next(i for i in read_rows(root / "open_sample/b/rep-1/ideas.jsonl") if i["text"] == "SHARED")
        a_shared = next(i for i in read_rows(root / "open_sample/a/rep-1/ideas.jsonl") if i["text"] == "SHARED")
        self.assertEqual(table[-1]["idea_ids"], [b_shared["idea_id"], a_shared["idea_id"]])

    def test_other_unit_baseline_cannot_trigger_stop_on_exhaustion(self):
        for order in (("a", "b"), ("b", "a")):
            root = self.root / "stop_" / "".join(order)
            for name in order:
                config = validate_config({"name": name, "default_model": name, "agents": 1,
                                          "stop_on_exhaustion": True, "max_batches": 2})
                outputs = [response("overlap" if name == "a" else "base")] * 3
                outputs += [response("fresh" if name == "a" else "overlap")] * 2
                self.api(root, config=config, backend=FakeBackend(responses=outputs))
                rows = read_rows(root / f"open_sample/{name}/rep-1/loop_batches.jsonl")[3:]
                self.assertEqual([r["new_mechanisms"] for r in rows], [1, 0])
                self.assertEqual(rows[-1]["stop_reason"], "stop_on_exhaustion")

    def test_second_rep_prompts_match_isolated_rep_and_keep_local_singletons(self):
        expected = None
        for earlier in (False, True):
            root = self.root / str(earlier)
            config = validate_config({"name": "unit", "default_model": "generator", "agents": 1,
                                      "levels": ["round1", "round2", "pressure3"]})
            if earlier:
                self.api(root, config=config, backend=FakeBackend(responses=[response("REP1_BASE")] * 3 +
                    [response("SHARED", decomposition=["REP1_DIM"]), response("REP1_SECRET"), response("REP1_LAST")]))
            prompts = []
            outputs = iter([response("REP2_BASE")] * 3 + [response("SHARED", decomposition=["REP2_DIM"]),
                                                        response("REP2_SECOND"), response("REP2_LAST")])
            def generated(prompt, *_):
                prompts.append(prompt)
                return next(outputs)
            self.api(root, config=config, rep=2, backend=FakeBackend(responses=generated))
            self.assertTrue(all("REP1_" not in p for p in prompts))
            later = next(p for p in prompts if "Generation level: round2." in p)
            from evals.brainstorm.tests.test_loop import data_after
            self.assertEqual(data_after(later, "Earlier agents' ideas:"), [{"title": "SHARED", "text": "SHARED"}])
            if expected is None:
                expected = prompts
            else:
                self.assertEqual(prompts, expected)

    def test_changed_identity_or_classifier_rejected_before_new_calls(self):
        root = self.root / "run"
        self.api(root)
        events = read_rows(root / "ledger")
        for changed in (dict(seed=20), dict(config=validate_config({"name": "unit", "default_model": "changed"})),
                        dict(classifier=validate_config({"name": "fixed", "default_model": "changed"})),
                        dict(exam={**load_exams(SAMPLES / "open")[0], "question": "changed"})):
            with self.subTest(changed=list(changed)), self.assertRaisesRegex(ValueError, "identity conflict"):
                self.api(root, **changed)
        fresh = FakeBackend(responses=[])
        config = validate_config({"name": "other", "default_model": "other"})
        with self.assertRaisesRegex(ValueError, "classifier identity conflict"):
            self.api(root, config=config, classifier=validate_config({"name": "fixed", "default_model": "changed"}), backend=fresh)
        self.assertEqual(fresh.calls, 0)
        self.assertEqual(read_rows(root / "ledger"), events)

    def test_pre_isolation_loop_policy_is_not_silently_resumed(self):
        root = self.root / "old_policy"
        self.api(root)
        file = root / "open_sample/unit/rep-1/loop_state.json"
        envelope = read_file(file)
        envelope["state"]["identity"].pop("loop_policy", None)
        envelope["digest"] = loop.record.digest(envelope["state"])
        file.write_text(json.dumps(envelope))
        events = read_rows(root / "ledger")
        fresh = FakeBackend(responses=[])
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            self.api(root, backend=fresh)
        self.assertEqual(fresh.calls, 0)
        self.assertEqual(read_rows(root / "ledger"), events)

    def test_fake_and_real_mix_rejected_without_calls(self):
        fake, other = FakeBackend(), FakeBackend()
        other.name = "other"
        with self.assertRaisesRegex(ValueError, "mix fake and real"):
            self.api(self.root / "mixed", backend=fake, grouping=other)
        self.assertEqual((fake.calls, other.calls), (0, 0))

    def test_terminal_generation_failures_and_unknown_do_not_advance_or_retry(self):
        for failure, responses, expected_calls in (("paid_error", [response("base")], 1),
                ("unknown", None, 1), (None, ["bad", "bad"], 2), ("not_started", None, 0)):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                backend = FakeBackend(failure=failure, responses=responses)
                with self.assertRaises((ValueError, CallUncertain, RuntimeError)):
                    self.api(root, backend=backend)
                self.assertEqual(backend.calls, expected_calls)
                rows = read_rows(root / "open_sample/unit/rep-1/loop_batches.jsonl")
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]["new_mechanisms"])
                if failure != "not_started":
                    fresh = FakeBackend(responses=[])
                    with self.assertRaises((ValueError, CallUncertain)):
                        self.api(root, backend=fresh)
                    self.assertEqual(fresh.calls, 0)
                events = read_rows(root / "ledger")
                if failure == "unknown":
                    self.assertEqual(events[-1]["event"], "uncertain")
                elif failure == "not_started":
                    self.assertEqual(events[-1]["event"], "release")
                else:
                    self.assertEqual(sum(e["usd"] for e in events if e["event"] == "settle"), Decimal(".01") * expected_calls)

    def test_actual_over_reserve_is_charged_and_larger_limit_does_not_unhalt(self):
        root = self.root / "over"
        backend = FakeBackend(usd=".03", reserve_usd=".02", responses=[response("base")])
        with self.assertRaises(BudgetExceeded):
            self.api(root, backend=backend)
        self.assertEqual(backend.calls, 1)
        events = read_rows(root / "ledger")
        self.assertEqual(sum(e["usd"] for e in events if e["event"] == "settle"), Decimal(".03"))
        fresh = FakeBackend(responses=[])
        with self.assertRaises(BudgetExceeded):
            self.api(root, backend=fresh, limit="100")
        self.assertEqual(fresh.calls, 0)

    def test_missing_state_corrupt_digest_and_conflicting_projection_are_not_reset(self):
        for mode in ("missing_loop", "digest", "projection", "missing_record"):
            with self.subTest(mode=mode):
                root = self.root / mode
                self.api(root)
                unit = root / "open_sample/unit/rep-1"
                if mode == "missing_loop":
                    (unit / "loop_state.json").unlink()
                elif mode == "digest":
                    payload = read_file(unit / "loop_state.json")
                    payload["digest"] = "bad"
                    (unit / "loop_state.json").write_text(json.dumps(payload))
                elif mode == "projection":
                    (unit / "loop_batches.jsonl").write_text('{"batch":999}\n')
                else:
                    (unit / "record_state.json").unlink()
                fresh = FakeBackend(responses=[])
                with self.assertRaises(ValueError):
                    self.api(root, backend=fresh)
                self.assertEqual(fresh.calls, 0)

    def test_plain_recording_outputs_are_not_adopted_by_loop(self):
        root = self.root / "plain"
        config = validate_config({"name": "unit", "default_model": "generator", "agents": 1})
        record_batch(load_exams(SAMPLES / "open")[0], config, root, seed=19,
                     backend=FakeBackend(responses=[response("plain")]), ledger=BudgetLedger(root / "ledger"))
        fresh = FakeBackend(responses=[])
        with self.assertRaisesRegex(ValueError, "Missing loop state"):
            self.api(root, config=config, backend=fresh)
        self.assertEqual(fresh.calls, 0)

    def test_cli_budget_exit_three_and_invalid_arguments(self):
        for limit, count, stage in ((".01", 0, "generate"), (".02", 1, "dedupe"), (".07", 6, "generate"),
                                    (".08", 7, "dedupe"), (".09", 8, "rewrite"), (".10", 9, "rewrite")):
            with self.subTest(limit=limit):
                name = "low_" + limit
                result = subprocess.run(self.command(name, "--limit", limit), capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("stage=" + stage, result.stderr)
                self.assertIn("rerun the same arguments", result.stderr)
                self.assertIn("raising --limit cannot clear", result.stderr)
                self.assertEqual(len([e for e in read_rows(self.root / name / "ledger.fake.jsonl") if e["event"] == "settle"]), count)
        help_result = subprocess.run([sys.executable, "-m", "evals.brainstorm", "loop", "--help"], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--dedupe-config", help_result.stdout)
        bad = subprocess.run(self.command("bad", "--rep", "0"), capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)

    def test_second_agent_rewrite_budget_reports_one_pending_assignment(self):
        config = validate_config({"name": "unit", "default_model": "generator", "agents": 2,
                                  "return_collisions": True})
        backend = FakeBackend(responses=lambda p, *_: returned(p, "replacement") if "Collision return:" in p else response("base"))
        # Each agent's return is its own call and both are grouped once afterwards, so the second agent's call is the one
        # a .14 limit stops: .13 used plus its .02 reserve.
        with self.assertRaisesRegex(BudgetExceeded, "pending_agents=1;"):
            self.api(self.root / "second_return", config=config, backend=backend, limit=".14")

    def test_pressure_two_and_four_resume_keep_frozen_assignments_and_charges(self):
        exam = {**load_exams(SAMPLES / "open")[0], "card_data": [
            {"id": f"c{n}", "text": f"card {n}"} for n in range(100)]}
        config = validate_config({"name": "unit", "default_model": "generator", "agents": 2,
            "levels": ["pressure2", "pressure4"], "materials_mode": "proportion", "materials_proportion": .05,
            "method_mode": "different", "methods": {f"m{n}": f"method {n}" for n in range(8)}})
        def generated(prompt, *_):
            level = prompt.split("Generation level: ", 1)[1].split(".", 1)[0]
            current = read_rows(active_root / "open_sample/unit/rep-1/loop_batches.jsonl")[-1]
            self.assertIsNone(current["stop_reason"])
            return response(level)
        reference = self.root / "pressure_reference"
        active_root = reference
        self.api(reference, config=config, exam=exam, backend=FakeBackend(responses=generated))
        expected = self.snapshot(reference)
        self.assertEqual((expected["calls"], expected["usd"]), (15, Decimal(".15")))
        for level, limit in (("pressure2", ".11"), ("pressure4", ".14")):
            with self.subTest(level=level):
                root = self.root / level
                active_root = root
                with self.assertRaises(BudgetExceeded):
                    self.api(root, config=config, exam=exam, backend=FakeBackend(responses=generated), limit=limit)
                unit = root / "open_sample/unit/rep-1"
                pending = read_file(unit / "loop_state.json")["state"]["batches"][-1]
                self.assertEqual(pending["level"], level)
                frozen = pending["inputs"]["assignments"]
                recorded = read_file(unit / "record_state.json")["state"]
                self.assertEqual(recorded["batches"][f"{pending['batch']}:{level}"]["assignments"], frozen)
                self.assertEqual(len([i for i in recorded["ideas"] if i["level"] == level]), 1)
                self.api(root, config=config, exam=exam, backend=FakeBackend(responses=generated))
                self.assertEqual(self.snapshot(root), expected)
                actual = read_file(unit / "record_state.json")["state"]["batches"][f"{pending['batch']}:{level}"]["assignments"]
                self.assertEqual(actual, frozen)

    def test_cli_subprocess_guards_block_real_backends_and_network(self):
        for code in ("from evals.brainstorm.backends import ClaudeCliBackend; ClaudeCliBackend.call(None, '', 'm', 1)",
                     "from evals.brainstorm.backends import CommandBackend; CommandBackend.call(None, '', 'm', 1)",
                     "import socket; socket.socket().connect(('127.0.0.1', 9))"):
            with self.subTest(code=code):
                result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("LOOP_TEST_EXTERNAL_CALL_BLOCKED", result.stderr)
        self.assertEqual(self.guard_log.read_text().splitlines(), ["active", "blocked"] * 3)

    def test_two_processes_serialize_same_unit_and_seed_reproduces_allocation(self):
        commands = [subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        for process in commands:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
        events = [e for e in read_rows(self.root / "run/ledger.fake.jsonl") if e["event"] == "settle"]
        self.assertEqual(len(events), 14)
        self.assertEqual(len({e["call_key"] for e in events}), 14)
        result = subprocess.run(self.command("independent"), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        a = read_file(self.root / "run/open_sample/cli/rep-1/run_summary.json")["allocations"]
        b = read_file(self.root / "independent/open_sample/cli/rep-1/run_summary.json")["allocations"]
        self.assertEqual(a, b)

    def test_historical_fallback_does_not_mark_current_batch_incomplete(self):
        root = self.root / "history"
        old = validate_config({"name": "old", "default_model": "old", "agents": 1})
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.api(root, config=old, grouping=FakeBackend(responses=["{}"]))
        self.api(root)
        row = read_rows(root / "open_sample/unit/rep-1/loop_batches.jsonl")[-1]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["fallback_ids"], [])
        self.assertEqual(row["new_mechanisms"], 1)

    def test_paid_or_unknown_dedupe_never_retries_or_generates_next_batch(self):
        for failure in ("paid_error", "unknown"):
            with self.subTest(failure=failure):
                root = self.root / failure
                backend, grouping = FakeBackend(responses=[response("base")]), FakeBackend(failure=failure)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.api(root, backend=backend, grouping=grouping)
                self.assertEqual((backend.calls, grouping.calls), (1, 1))
                fresh, fresh_group = FakeBackend(responses=[]), FakeBackend(responses=[])
                with self.assertRaises((ValueError, RuntimeError)):
                    self.api(root, backend=fresh, grouping=fresh_group)
                self.assertEqual((fresh.calls, fresh_group.calls), (0, 0))

    def test_projection_write_failure_recovers_without_repeating_settled_calls(self):
        root = self.root / "io_failure"
        write = loop.atomic_write
        failed = []
        def fail_projection(path, text):
            if path.name == "loop_batches.jsonl" and '"stage"' not in text and '"new_mechanisms":1' in text and not failed:
                failed.append(True)
                raise OSError("Synthetic projection disk failure")
            return write(path, text)
        with patch.object(loop, "atomic_write", side_effect=fail_projection), self.assertRaises(OSError):
            self.api(root)
        self.assertEqual(failed, [True])
        before = read_rows(root / "ledger")
        self.api(root)
        self.assertEqual(read_rows(root / "ledger"), before)
        self.assertEqual(read_rows(root / "open_sample/unit/rep-1/loop_batches.jsonl")[-1]["new_mechanisms"], 1)

    def test_partial_generation_failure_projection_keeps_already_recorded_ids(self):
        root = self.root / "partial"
        config = validate_config({"name": "unit", "default_model": "generator", "agents": 2})
        with self.assertRaises(BudgetExceeded):
            self.api(root, config=config, limit=".02", backend=FakeBackend(responses=[response("base")]))
        unit = root / "open_sample/unit/rep-1"
        ids = [r["idea_id"] for r in read_rows(unit / "ideas.jsonl")]
        self.assertEqual(len(ids), 1)
        batch = read_rows(unit / "loop_batches.jsonl")[0]
        self.assertEqual(batch["idea_ids"], ids)
        self.assertIsNone(batch["new_mechanisms"])

    def test_repeated_projection_failure_keeps_last_durable_projection_recoverable(self):
        root = self.root / "repeated_io"
        write = loop.atomic_write
        def fail_projection(path, text):
            if path.name == "loop_batches.jsonl" and '"idea_ids":["i-' in text:
                raise OSError("Projection still unavailable")
            return write(path, text)
        with patch.object(loop, "atomic_write", side_effect=fail_projection):
            for _ in range(2):
                with self.assertRaises(OSError):
                    self.api(root)
        self.api(root)
        self.assertEqual(len([e for e in read_rows(root / "ledger") if e["event"] == "settle"]), 10)
        self.assertEqual(read_rows(root / "open_sample/unit/rep-1/loop_batches.jsonl")[-1]["new_mechanisms"], 1)
