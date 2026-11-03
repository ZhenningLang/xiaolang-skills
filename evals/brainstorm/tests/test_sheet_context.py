"""The exam question and hard constraints reach both page kinds unchanged, and nothing else comes with them."""

import json
import subprocess
import sys
import unittest

from evals.brainstorm import serve, sheet
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger
from evals.brainstorm.tests.test_page_source import PAGE
from evals.brainstorm.tests.test_serve import LINEAGE, ServerCase
from evals.brainstorm.tests.test_sheet import A, ANSWER, B, EXAM_ID, FIXTURE, ROOT, copy_run, public_of


QUESTION = "  怎么让共用架子\n更好用？<b>原样显示</b>  "
CONSTRAINTS = ["不能加人", "两周内上线"]
HIDDEN = {"hidden_answers": ["HIDDEN_ANSWER_CANARY"], "wrong_premise": "WRONG_PREMISE_CANARY",
          "leak_check": "LEAK_CHECK_CANARY", "license": "LICENSE_CANARY"}


class ContextCase(ServerCase):
    def setUp(self):
        super().setUp()
        self.exam = {**self.exam, **HIDDEN, "question": QUESTION, "constraints": CONSTRAINTS}

    def issue_kind(self, kind):
        """One sheet of each kind the page can open."""
        options = {"all": {"kind": "score", "all_view": True},
                   "compare": {"kind": "score", "configs": [A, B], "per_unit": 6, "backend": FakeBackend(responses=sheet.fake_rewrite),
                               "ledger": BudgetLedger(self.root / "ledger.jsonl", "50"), "model": "rewrite_model"},
                   "group": {"kind": "group", "count": 10}}[kind]
        return sheet.make_sheet(self.run_dir, self.exam, sheet_id="s_ctx", seed=7, **options)

    def strip_to_old_format(self, sheet_id="s_ctx"):
        path = self.run_dir / "sheets" / sheet_id / "sheet.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["question"], data["constraints"]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class ResponseTests(ContextCase):
    def check_kind(self, kind):
        self.issue_kind(kind)
        stored = public_of(self.run_dir, "s_ctx")
        self.assertEqual(set(stored), {"sheet_id", "kind", "question", "constraints", "items"})
        self.assertEqual((stored["question"], stored["constraints"]), (QUESTION, CONSTRAINTS))
        self.start("s_ctx")
        status, body, _ = self.get("/api/sheet")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual((state["question"], state["constraints"]), (QUESTION, CONSTRAINTS))
        self.assertEqual(set(state), {"sheet_id", "kind", "question", "constraints", "items", "answers", "progress"})
        for item in state["items"]:
            self.assertEqual(set(item), {"item_id", "text", "variants"})
        text = body.decode()
        self.assertIsNone(LINEAGE.search(text), text[:400])
        for canary in (*HIDDEN["hidden_answers"], HIDDEN["wrong_premise"], HIDDEN["leak_check"], HIDDEN["license"], EXAM_ID):
            self.assertNotIn(canary, text)

    def test_full_score_sheet_carries_the_question(self):
        self.check_kind("all")

    def test_comparison_score_sheet_carries_the_question(self):
        self.check_kind("compare")

    def test_group_sheet_carries_the_question(self):
        self.check_kind("group")

    def test_question_that_names_a_source_is_kept_as_written_and_reported(self):
        self.exam = {**self.exam, "question": f"{A} 这个词也算题目"}
        result = self.issue_kind("group")
        self.assertEqual(public_of(self.run_dir, "s_ctx")["question"], f"{A} 这个词也算题目")
        self.assertTrue(any("question" in text and "config" in text for text in result["warnings"]), result["warnings"])

    def test_clean_question_gives_no_warning(self):
        self.assertFalse([text for text in self.issue_kind("group")["warnings"] if "question" in text])


class RefusalTests(ContextCase):
    def assert_refused(self, exam, pattern, kind="group"):
        with self.assertRaisesRegex(ValueError, pattern):
            sheet.make_sheet(self.run_dir, exam, kind=kind, sheet_id="s_ctx", seed=7, count=5)
        self.assertFalse((self.run_dir / "sheets/s_ctx/sheet.json").exists())

    def test_exam_without_a_question_issues_no_sheet(self):
        for exam in ({"id": EXAM_ID}, {**self.exam, "question": "   "}, {**self.exam, "question": None}):
            with self.subTest(exam.get("question")):
                self.assert_refused(exam, "question")

    def test_exam_with_bad_constraints_issues_no_sheet(self):
        for constraints in (None, "不能加人", [""], [1]):
            with self.subTest(constraints):
                self.assert_refused({**self.exam, "constraints": constraints}, "constraints")

    def test_group_cli_needs_the_exam_dir(self):
        result = subprocess.run([sys.executable, "-m", "evals.brainstorm", "sheet", "--kind", "group", "--run-dir", str(self.run_dir),
                                 "--exam-id", EXAM_ID, "--sheet-id", "s_ctx", "--seed", "1"],
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn("--exam-dir", result.stderr)
        self.assertFalse((self.run_dir / "sheets/s_ctx/sheet.json").exists())

    def test_group_cli_takes_the_question_from_the_exam_dir(self):
        result = subprocess.run([sys.executable, "-m", "evals.brainstorm", "sheet", "--kind", "group", "--run-dir", str(self.run_dir),
                                 "--exam-dir", str(FIXTURE / "exams"), "--exam-id", EXAM_ID, "--sheet-id", "s_ctx", "--seed", "1"],
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(public_of(self.run_dir, "s_ctx")["question"], "测试用：怎么让共用架子更好用？")

    def test_half_written_context_refuses_to_start(self):
        self.issue_kind("group")
        path = self.run_dir / "sheets/s_ctx/sheet.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        only_question = {key: value for key, value in data.items() if key != "constraints"}
        only_constraints = {key: value for key, value in data.items() if key != "question"}
        for broken in (only_question, only_constraints, {**data, "question": None}, {**data, "question": " "},
                       {**data, "constraints": None}, {**data, "constraints": [1]}):
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unexpected"):
                serve.make_server(self.run_dir, "s_ctx")


class OldSheetTests(ContextCase):
    def test_old_sheet_opens_with_an_explicit_empty_question(self):
        for kind in ("group", "all"):
            with self.subTest(kind):
                self.run_dir = copy_run(self.root, f"old_{kind}")
                self.issue_kind(kind)
                self.strip_to_old_format()
                self.start("s_ctx")
                state = json.loads(self.get("/api/sheet")[1])
                self.assertIsNone(state["question"])
                self.assertEqual(state["constraints"], [])
                self.assertTrue(state["items"])

    def test_old_sheet_can_still_be_finished_and_revealed(self):
        """Score sheets block a new one for the exam until revealed, so an old sheet must not become a dead end."""
        self.issue_kind("all")
        self.strip_to_old_format()
        items = public_of(self.run_dir, "s_ctx")["items"]
        sheet.merge_answers(self.run_dir, "s_ctx", {"sheet_id": "s_ctx", "answers": [{"item_id": row["item_id"], **ANSWER} for row in items]})
        self.assertEqual(sheet.reveal(self.run_dir, "s_ctx")["kind"], "score")
        sheet.make_sheet(self.run_dir, self.exam, kind="score", sheet_id="s_next", seed=8, all_view=True)

    def test_serve_command_warns_on_an_old_sheet_and_not_on_a_new_one(self):
        self.issue_kind("group")
        def stderr_of_serve():
            process = subprocess.Popen([sys.executable, "-m", "evals.brainstorm", "serve", "--run-dir", str(self.run_dir), "--sheet-id", "s_ctx"],
                                       cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertTrue(process.stdout.readline().startswith("http://127.0.0.1:"))
            process.terminate()
            _, stderr = process.communicate(timeout=10)
            return stderr
        self.assertNotIn("no question", stderr_of_serve())
        self.strip_to_old_format()
        self.assertIn("no question", stderr_of_serve())


class PageCopyTests(unittest.TestCase):
    def test_page_renders_the_context_and_the_old_sheet_notice(self):
        for name in ("exam_question", "exam_constraints", "no_question", "state.question", "state.constraints"):
            self.assertIn(name, PAGE)

    def test_group_copy_names_the_rule_and_the_purpose(self):
        self.assertIn("做法相同的放一起，说法不同没关系；拿不准就分开", PAGE)
        self.assertIn("这张卷用来核对程序合并点子是否准确", PAGE)
        self.assertNotIn("意思一样的放一起", PAGE)
