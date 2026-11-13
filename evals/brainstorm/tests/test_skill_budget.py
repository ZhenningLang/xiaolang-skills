"""Tests for skill_budget.py: the equal-call control run, the merge under one grouper, and the smoke check."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
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

    def drive(self, limit=100, run=None):
        actions = []
        for _ in range(limit):
            code, action = call("next", "--dir", str(run or self.control))
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


    def merged_run(self):
        call("direct-init", "--dir", str(self.control), "--question-file", str(QUESTION), "--batches", "2",
             "--constraint", "no paid tools", "--constraint", "two hours a week")
        self.drive()
        done = self.skill_run()
        call("merge", "--dir", str(self.control), "--from", str(self.skill))
        self.drive()
        return done

    def test_audit_prompt_is_blind_and_samples_only_what_the_skill_alone_reached(self):
        self.assertEqual(call("audit-init", "--dir", str(self.control))[0], 1)  # nothing to audit before a merge
        done = self.merged_run()
        code, made = call("audit-init", "--dir", str(self.control), "--sample", "4", "--seed", "3")
        self.assertEqual(code, 0)
        prompt = Path(made["prompt"]).read_text(encoding="utf-8")
        key = json.loads((self.control / "audit" / "key.json").read_text(encoding="utf-8"))
        state = json.loads((self.control / "state.json").read_text(encoding="utf-8"))
        by_id = {row["mechanism_id"]: row for row in state["mechanisms"]}
        self.assertEqual((len(key["A"]), len(key["B"]), len(key["C"])), (3, 4, 3 + done["beyond_baseline"]))
        for mechanism_id in key["B"].values():
            self.assertTrue(all(idea.startswith("x-") for idea in by_id[mechanism_id]["idea_ids"]))
        self.assertEqual(made["skill_only"], done["beyond_baseline"])
        body = prompt.replace(made["answer"], "")  # the run directory's own name is not a source label
        for word in ("x-i-", "m-0", "skill", "control", "import"):
            self.assertNotIn(word, body)
        self.assertIn(sb.SAME_RULE, prompt)
        self.assertIn("1. no paid tools", prompt)
        self.assertIn(made["answer"], prompt)
        again = call("audit-init", "--dir", str(self.control), "--sample", "4", "--seed", "3")[1]
        self.assertEqual(json.loads((self.control / "audit" / "key.json").read_text(encoding="utf-8")), key)
        self.assertEqual(again["prompt"], made["prompt"])
        self.assertEqual(call("audit-init", "--dir", str(self.control), "--sample", "999")[1]["sampled"],
                         done["beyond_baseline"])

    def test_audit_result_counts_repeats_and_violations_and_refuses_a_partial_answer(self):
        done = self.merged_run()
        made = call("audit-init", "--dir", str(self.control), "--sample", "4", "--seed", "3")[1]
        self.assertEqual(call("audit-read", "--dir", str(self.control))[0], 1)  # no answer yet
        key = json.loads((self.control / "audit" / "key.json").read_text(encoding="utf-8"))
        skill_side = next(label for label, row in key["C"].items() if row["side"] == "skill_only")
        answer = {"repeats": [{"b": label, "same_as": "A2" if label == "B1" else None, "note": ""} for label in key["B"]],
                  "constraints": [{"c": label, "violated": [2] if label == skill_side else [], "note": ""}
                                  for label in key["C"]]}
        Path(made["answer"]).write_text(json.dumps(answer), encoding="utf-8")
        code, result = call("audit-read", "--dir", str(self.control))
        self.assertEqual(code, 0)
        self.assertEqual((result["repeats"]["sampled"], result["repeats"]["judged_same"]), (4, 1))
        self.assertEqual(result["repeats"]["skill_only_by_grouper"], done["beyond_baseline"])
        self.assertEqual(result["repeats"]["estimated_new"], round(done["beyond_baseline"] * 3 / 4))
        self.assertEqual(result["constraints"]["checked"], 3 + done["beyond_baseline"])
        self.assertEqual(result["constraints"]["violations"], {"skill_only": 1, "control_only": 0, "both": 0})
        self.assertEqual(result["rule"], sb.SAME_RULE)
        answer["repeats"][0]["same_as"] = "A9"
        answer["constraints"].pop()
        answer["constraints"][0]["violated"] = [3]
        Path(made["answer"]).write_text(json.dumps(answer), encoding="utf-8")
        code, result = call("audit-read", "--dir", str(self.control))
        self.assertEqual(code, 1)
        self.assertEqual(len(result["problems"]), 3)

    def two_groupings(self):
        call("direct-init", "--dir", str(self.control), "--question-file", str(QUESTION), "--batches", "2")
        self.drive()
        done = self.skill_run()
        second = Path(self.tmp.name) / "second"
        shutil.copytree(self.control, second)
        for run in (self.control, second):
            call("merge", "--dir", str(run), "--from", str(self.skill))
            self.drive(run=run)
        return second, done

    def test_repeat_compares_groupings_of_the_same_ideas(self):
        second, done = self.two_groupings()
        code, result = call("repeat", "--dir", str(self.control), "--dir", str(second))
        self.assertEqual(code, 0)
        self.assertEqual([row["skill_only"] for row in result["runs"]], [done["beyond_baseline"]] * 2)
        self.assertEqual(result["skill_only_range"], [done["beyond_baseline"]] * 2)
        pair = result["pairs"][0]
        self.assertEqual((pair["ideas"], pair["same_verdict"], pair["rate"]), (done["ideas"], done["ideas"], 1.0))
        new = result["runs"][0]["ideas_outside_control"]
        self.assertEqual(result["ideas"], {"total": done["ideas"], "new_in_every_grouping": new,
                                           "new_in_none": done["ideas"] - new, "split": 0})
        # Move one idea that only the skill reached into a mechanism the control also reached.
        state = json.loads((second / "state.json").read_text(encoding="utf-8"))
        ideas = state["ideas"]
        alone = next(row for row in state["mechanisms"] if all(key.startswith("x-") for key in row["idea_ids"]))
        shared = next(row for row in state["mechanisms"] if ideas[row["idea_ids"][0]]["level"] == "baseline")
        moved = len(alone["idea_ids"])
        shared["idea_ids"] += alone["idea_ids"]
        state["mechanisms"].remove(alone)
        (second / "state.json").write_text(json.dumps(state), encoding="utf-8")
        code, result = call("repeat", "--dir", str(self.control), "--dir", str(second))
        self.assertEqual(code, 0)
        self.assertEqual(result["skill_only_range"], [done["beyond_baseline"] - 1, done["beyond_baseline"]])
        self.assertEqual(result["pairs"][0]["same_verdict"], done["ideas"] - moved)
        self.assertEqual((result["pairs"][0]["new_in_first_only"], result["pairs"][0]["new_in_second_only"]), (moved, 0))
        self.assertEqual(result["ideas"]["split"], moved)

    def test_repeat_refuses_one_run_an_unfinished_merge_or_another_skill_run(self):
        second, _ = self.two_groupings()
        self.assertEqual(call("repeat", "--dir", str(self.control))[0], 1)
        state = json.loads((second / "state.json").read_text(encoding="utf-8"))
        state["merge"]["done"] = False
        (second / "state.json").write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(call("repeat", "--dir", str(self.control), "--dir", str(second))[0], 1)
        state["merge"]["done"] = True
        key = next(key for key in state["ideas"] if key.startswith("x-"))
        state["ideas"]["x-other"] = {**state["ideas"].pop(key), "idea_id": "x-other"}
        for row in state["mechanisms"]:
            row["idea_ids"] = ["x-other" if item == key else item for item in row["idea_ids"]]
        (second / "state.json").write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(call("repeat", "--dir", str(self.control), "--dir", str(second))[0], 1)


if __name__ == "__main__":
    unittest.main()
