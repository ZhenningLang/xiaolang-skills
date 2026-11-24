"""Tests of the think-compare evaluation. No model is called: a fake claude and a fake judge stand in.

    python3 -m unittest discover -s evals/compare/tests -t .
"""

import json
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from evals.compare import run

FAKE_CLAUDE = textwrap.dedent("""\
    import json, sys
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    skill = "--append-system-prompt" in argv
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "--setting-sources" in argv and argv[argv.index("--setting-sources") + 1] == ""
    text = ("WITH SKILL: " if skill else "PLAIN: ") + prompt[:20]
    print(json.dumps({"result": text, "is_error": False, "total_cost_usd": 0.01,
                      "usage": {"output_tokens": 5}, "modelUsage": {"fake-model": {}}}))
""")

# Prefers whichever answer starts with "WITH SKILL", and scores it higher on C5.
FAKE_JUDGE = textwrap.dedent("""\
    import json, re, sys
    prompt = sys.stdin.read()
    a = prompt.split("## Answer A")[1].split("## Answer B")[0]
    skill_is_a = "WITH SKILL" in a
    good = {"C1": 2, "C2": 2, "C3": 2, "C4": 2, "C5": 2, "C6": 2, "notes": "good"}
    weak = dict(good, C5=0, notes="weak")
    print("```json")
    print(json.dumps({"A": good if skill_is_a else weak, "B": weak if skill_is_a else good,
                      "preference": "A" if skill_is_a else "B", "reason": "r"}))
    print("```")
""")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "claude.py").write_text(FAKE_CLAUDE)
        (self.tmp / "judge.py").write_text(FAKE_JUDGE)
        self.claude = self.tmp / "claude"
        self.claude.write_text(f"#!/bin/sh\nexec {sys.executable} {self.tmp / 'claude.py'} \"$@\"\n")
        self.claude.chmod(0o755)
        self.run_dir = self.tmp / "run"

    def answer(self, arm, questions="database,cache"):
        return run.main(["answer", "--run-dir", str(self.run_dir), "--arm", arm, "--questions", questions,
                         "--claude", str(self.claude), "--model", "fake-model"])

    def judge(self, questions="database,cache"):
        return run.main(["judge", "--run-dir", str(self.run_dir), "--questions", questions,
                         "--judge-cmd", f"{sys.executable} {self.tmp / 'judge.py'}", "--judge-label", "fake-judge"])


class TestQuestions(unittest.TestCase):
    def test_eight_questions_with_unique_ids(self):
        questions = run.load_questions()
        self.assertEqual(len(questions), 8)
        self.assertEqual(len({q["id"] for q in questions}), 8)

    def test_unknown_id_is_refused(self):
        with self.assertRaises(SystemExit):
            run.load_questions(only="database,nope")


class TestClaudeArgv(unittest.TestCase):
    def test_plain_arm_has_no_skill(self):
        argv = run.claude_argv("claude", "m", "plain", "SKILL TEXT")
        self.assertNotIn("--append-system-prompt", argv)
        self.assertFalse(any("SKILL TEXT" in a for a in argv))

    def test_skill_arm_appends_the_skill(self):
        argv = run.claude_argv("claude", "m", "skill", "SKILL TEXT")
        self.assertIn("SKILL TEXT", argv[argv.index("--append-system-prompt") + 1])

    def test_both_arms_isolated(self):
        for arm in run.ARMS:
            argv = run.claude_argv("claude", "m", arm, "x")
            for flag in ("--disable-slash-commands", "--strict-mcp-config", "--no-session-persistence"):
                self.assertIn(flag, argv)


class TestBlindOrders(unittest.TestCase):
    def test_second_order_swaps_labels(self):
        for qid in ("database", "cache", "search"):
            first, second = run.mapping(qid, "1", "first"), run.mapping(qid, "1", "second")
            self.assertEqual(set(first.values()), {"skill", "plain"})
            self.assertEqual(first["A"], second["B"])
            self.assertEqual(first["B"], second["A"])

    def test_prompt_does_not_name_the_arms(self):
        rubric = run.RUBRIC.read_text()
        prompt = run.judge_prompt(rubric, "Q?", "answer one", "answer two")
        # "team skills" in the rubric is about the people asking, not about the arms
        for word in (r"\bskill\b", r"\bplain\b", "think-compare", "without"):
            self.assertIsNone(re.search(word, prompt.lower()), word)


class TestParseJudgment(unittest.TestCase):
    GOOD = {"A": {c: 1 for c in run.CRITERIA}, "B": {c: 2 for c in run.CRITERIA}, "preference": "B"}

    def test_fenced_json(self):
        reply = "Here you go:\n```json\n" + json.dumps(self.GOOD) + "\n```\n"
        self.assertEqual(run.parse_judgment(reply)["preference"], "B")

    def test_bare_json_with_text_around(self):
        self.assertEqual(run.parse_judgment("ok " + json.dumps(self.GOOD) + " done")["B"]["C1"], 2)

    def test_out_of_range_score_is_refused(self):
        bad = json.loads(json.dumps(self.GOOD))
        bad["A"]["C3"] = 3
        with self.assertRaises(ValueError):
            run.parse_judgment(json.dumps(bad))

    def test_boolean_score_is_refused(self):
        bad = json.loads(json.dumps(self.GOOD))
        bad["A"]["C3"] = True
        with self.assertRaises(ValueError):
            run.parse_judgment(json.dumps(bad))

    def test_missing_preference_is_refused(self):
        bad = dict(self.GOOD)
        del bad["preference"]
        with self.assertRaises(ValueError):
            run.parse_judgment(json.dumps(bad))

    def test_no_json(self):
        with self.assertRaises(ValueError):
            run.parse_judgment("I prefer A.")


class TestEndToEnd(Fixture):
    def test_answer_judge_summarize(self):
        self.assertEqual(self.answer("skill"), 0)
        self.assertEqual(self.answer("plain"), 0)
        record = json.loads((self.run_dir / "answers" / "database.skill.json").read_text())
        self.assertTrue(record["answer"].startswith("WITH SKILL"))
        self.assertIsNotNone(record["skill_sha256"])
        self.assertEqual(self.judge(), 0)
        out = self.tmp / "result.json"
        self.assertEqual(run.main(["summarize", "--run-dir", str(self.run_dir), "--questions", "database,cache",
                                   "--out", str(out), "--label", "test"]), 0)
        data = json.loads(out.read_text())
        c = data["counts"]
        self.assertEqual(c["judgments"], 4)
        self.assertEqual(c["preferred_skill"], 4)
        self.assertEqual(c["questions_skill_both_orders"], 2)
        self.assertEqual(c["questions_separated"], 2)
        # both orders were used: the skill answer stood once under each label per question
        self.assertEqual(c["preferred_label_A"], 2)
        self.assertEqual(data["mean_scores"]["skill"]["total"], 12)
        self.assertEqual(data["mean_scores"]["plain"]["total"], 10)
        # every number can be traced: raw answers and judge replies are in the file
        self.assertIn("database.skill", data["answers"])
        self.assertIn("reply", data["judgments"]["cache.second"]["attempts"][-1])

    def test_rerun_keeps_finished_files(self):
        self.answer("skill", "database")
        path = self.run_dir / "answers" / "database.skill.json"
        path.write_text(json.dumps(dict(json.loads(path.read_text()), answer="KEPT")))
        self.answer("skill", "database")
        self.assertEqual(json.loads(path.read_text())["answer"], "KEPT")

    def test_judge_skips_question_without_both_answers(self):
        self.answer("skill", "database")
        self.assertEqual(self.judge("database"), 1)
        self.assertFalse((self.run_dir / "judgments" / "database.first.json").exists())

    def test_unusable_judge_reply_is_recorded_as_failed(self):
        self.answer("skill", "database")
        self.answer("plain", "database")
        (self.tmp / "judge.py").write_text("print('no json here')\n")
        self.assertEqual(self.judge("database"), 1)
        failed = json.loads((self.run_dir / "judgments" / "database.first.failed.json").read_text())
        self.assertEqual(len(failed["attempts"]), 2)


class TestSeparation(unittest.TestCase):
    def judgment(self, order, skill_label, skill_scores, plain_scores, pref):
        plain_label = "B" if skill_label == "A" else "A"
        return {"order": order, "skill_label": skill_label,
                "labels": {skill_label: "skill", plain_label: "plain"},
                "judgment": {skill_label: skill_scores, plain_label: plain_scores, "preference": pref}}

    def scores(self, c5):
        return dict({c: 2 for c in run.CRITERIA}, C5=c5)

    def test_split_preference_does_not_separate(self):
        rows = [self.judgment("first", "A", self.scores(2), self.scores(0), "A"),
                self.judgment("second", "B", self.scores(2), self.scores(0), "A")]
        result = run.summarize_question(rows)
        self.assertIsNone(result["preferred_both_orders"])
        self.assertFalse(result["separates"])

    def test_consistent_preference_with_matching_scores_separates(self):
        rows = [self.judgment("first", "A", self.scores(0), self.scores(2), "B"),
                self.judgment("second", "B", self.scores(0), self.scores(2), "A")]
        result = run.summarize_question(rows)
        self.assertEqual(result["preferred_both_orders"], "plain")
        self.assertTrue(result["separates"])

    def test_equal_scores_do_not_separate(self):
        rows = [self.judgment("first", "A", self.scores(2), self.scores(2), "A"),
                self.judgment("second", "B", self.scores(2), self.scores(2), "B")]
        self.assertFalse(run.summarize_question(rows)["separates"])


if __name__ == "__main__":
    unittest.main()
