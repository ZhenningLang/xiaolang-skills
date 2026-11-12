"""Tests for skill_budget.py: the equal-call control run, the merge under one grouper, and the smoke check."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from evals.brainstorm import skill_budget as sb

SKILL_TESTS = sb.SKILL_SCRIPT.parents[1] / "tests"
spec = importlib.util.spec_from_file_location("brainstorm_skill_tests", SKILL_TESTS / "test_brainstorm.py")
skill_tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill_tests)
QUESTION = SKILL_TESTS / "fixtures" / "question.txt"


def call(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = sb.main(list(argv))
    text = out.getvalue().strip()
    return code, json.loads(text) if text else None


class SkillBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control = Path(self.tmp.name) / "control"
        self.skill = Path(self.tmp.name) / "skill"
        self.fake = skill_tests.FakeHost(self.control)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, sb.bs, "BASELINE_BATCHES", sb.bs.BASELINE_BATCHES)

    def drive(self, limit=100):
        actions = []
        for _ in range(limit):
            code, action = call("next", "--dir", str(self.control))
            self.assertEqual(code, 0)
            actions.append(action)
            if action["action"] == "done":
                return actions
            for task in action["tasks"]:
                (self.fake.group if action["purpose"] == "group" else self.fake.generate)(task)
        self.fail("did not finish")

    def skill_run(self):
        host = skill_tests.FakeHost(self.skill)
        host.cli("init", "--dir", str(self.skill), "--question-file", str(QUESTION), "--seed", "7",
                 "--profile", "economy", "--cards", str(SKILL_TESTS / "fixtures" / "cards.jsonl"))
        return host.run()

    def test_control_run_is_plain_asking_at_the_requested_number_of_calls(self):
        self.assertEqual(call("direct-init", "--dir", str(self.control), "--question-file", str(QUESTION),
                              "--batches", "4", "--seed", "7")[0], 0)
        actions = self.drive()
        first = actions[0]
        self.assertEqual((first["purpose"], len(first["tasks"])), ("generate", 12))
        prompt = Path(first["tasks"][0]["prompt"]).read_text(encoding="utf-8")
        self.assertEqual(skill_tests.data_block(prompt, "Assigned cards:"), [])
        self.assertNotIn("Method instructions:", prompt)
        self.assertEqual([a.get("purpose") for a in actions[1:-1]], ["group"] * 4)  # one grouping per batch
        code, result = call("compare", "--dir", str(self.control))
        self.assertEqual(code, 0)
        self.assertEqual(result["control"]["idea_calls"], 12)
        self.assertEqual(result["control"]["ideas"], 36)
        # The fixture agents all give the same three ideas: everything new arrives in the first batch.
        self.assertEqual([row["mechanisms_so_far"] for row in result["control"]["curve"]], [3, 3, 3, 3])
        self.assertEqual([row["calls_so_far"] for row in result["control"]["curve"]], [3, 6, 9, 12])
        self.assertNotIn("same_grouper", result)

    def test_merge_counts_what_only_the_skill_reached(self):
        call("direct-init", "--dir", str(self.control), "--question-file", str(QUESTION), "--batches", "2")
        self.drive()
        done = self.skill_run()
        code, merged = call("merge", "--dir", str(self.control), "--from", str(self.skill))
        self.assertEqual((code, merged["merged_ideas"]), (0, done["ideas"]))
        code, waiting = call("next", "--dir", str(self.control))
        self.assertEqual(waiting["action"], "dispatch")
        before = (self.control / "state.json").read_text(encoding="utf-8")
        self.assertEqual(call("next", "--dir", str(self.control))[1]["action"], "wait")
        self.assertEqual((self.control / "state.json").read_text(encoding="utf-8"), before)
        self.fake.group(waiting["tasks"][0])
        result = self.drive()[-1]
        self.assertEqual(result["skill"]["mechanisms"], done["mechanisms"])
        same = result["same_grouper"]
        # The skill's own baseline repeats the control's three ideas; every later mechanism is the skill's alone.
        self.assertEqual((same["both"], same["control_only"], same["skill_only"]), (3, 0, done["beyond_baseline"]))
        self.assertEqual(sum(same["skill_only_by_first_level"].values()), done["beyond_baseline"])
        self.assertNotIn("baseline", same["skill_only_by_first_level"])
        self.assertEqual(result["queued_for_grouping"], 0)
        self.assertEqual(call("next", "--dir", str(self.control))[1]["action"], "done")

    def test_merge_refuses_an_unfinished_or_foreign_run(self):
        call("direct-init", "--dir", str(self.control), "--question-file", str(QUESTION), "--batches", "1")
        self.skill_run()
        self.assertEqual(call("merge", "--dir", str(self.control), "--from", str(self.skill))[0], 1)  # control unfinished
        self.drive()
        state = json.loads((self.skill / "state.json").read_text(encoding="utf-8"))
        state["question"] = "another question"
        (self.skill / "state.json").write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(call("merge", "--dir", str(self.control), "--from", str(self.skill))[0], 1)

    def test_smoke_check_passes_a_clean_run_and_names_what_failed(self):
        self.skill_run()
        code, result = call("check", "--dir", str(self.skill), "--min-new", "6")
        self.assertEqual((code, result["ok"]), (0, True))
        code, result = call("check", "--dir", str(self.skill), "--min-new", "50")
        self.assertEqual(code, 1)
        self.assertTrue(any("round1 added" in line for line in result["problems"]))
        state = json.loads((self.skill / "state.json").read_text(encoding="utf-8"))
        state["failures"].append({"task": "b004-a1", "reason": "missing_output", "detail": "x"})
        state["complete"] = False
        (self.skill / "state.json").write_text(json.dumps(state), encoding="utf-8")
        code, result = call("check", "--dir", str(self.skill), "--min-new", "6")
        self.assertEqual(code, 1)
        self.assertEqual(len(result["problems"]), 2)


if __name__ == "__main__":
    unittest.main()
