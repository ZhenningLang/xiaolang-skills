"""Static page rules and the sheet, serve, reveal command chain."""

import ast
import http.client
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from evals.brainstorm.tests.test_sheet import ANSWER, EXAM_ID, FIXTURE, ROOT, copy_run


WEB = ROOT / "evals/brainstorm/web"
PAGE = (WEB / "index.html").read_text(encoding="utf-8")
QUESTIONS = ("看这道题之前，你想到过吗？", "假设它成了，你在乎吗？", "你说得出它一定不成的原因吗？", "你想试吗？（只记录，不计分）")


class PageSourceTests(unittest.TestCase):
    def test_page_is_a_single_self_contained_file(self):
        self.assertEqual([path.name for path in WEB.iterdir()], ["index.html"])

    def test_no_remote_resources_or_html_injection_apis(self):
        for banned in ("http://", "https://", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "@import"):
            self.assertNotIn(banned, PAGE)

    def test_four_questions_keep_their_exact_wording(self):
        for question in QUESTIONS:
            self.assertEqual(PAGE.count(question), 1, question)

    def test_answer_direction_of_the_first_question_is_inverted(self):
        self.assertIn('first: ["想到过", false], second: ["没想到", true]', PAGE)
        self.assertIn('first: ["在乎", true], second: ["不在乎", false]', PAGE)
        self.assertIn('first: ["想试", true], second: ["不想试", false]', PAGE)

    def test_supports_both_color_schemes_and_phone_width(self):
        self.assertIn("prefers-color-scheme: dark", PAGE)
        self.assertIn("color-scheme: light dark", PAGE)
        self.assertIn('name="viewport"', PAGE)
        self.assertIn("min-height: 44px", PAGE)

    def test_copy_avoids_lineage_words_and_field_names(self):
        for word in ("机制", "配置", "模型", "方法", "血统", "idea_id", "mechanism_id", "config_reps", "representative", "check_bad"):
            self.assertNotIn(word, PAGE)

    def test_page_explains_the_masked_marker(self):
        self.assertIn("[已隐去]", PAGE)
        self.assertIn("mask_note", PAGE)

    def test_page_shows_no_score(self):
        for word in ("分数", "得分", "scored"):
            self.assertNotIn(word, PAGE)
        self.assertNotIn("score_", PAGE)

    def test_page_uses_no_leak_prone_identifier_prefix(self):
        self.assertIsNone(re.search(r"\b(think|guard|dev|write|readable|assist|fe)-[a-z]", PAGE))


class CommandChainTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.run_dir = copy_run(self.root)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "evals.brainstorm", *args], cwd=ROOT, capture_output=True, text=True, timeout=60)

    def test_sheet_serve_and_reveal_work_as_real_processes(self):
        issued = self.cli("sheet", "--kind", "score", "--all", "--run-dir", str(self.run_dir), "--exam-dir", str(FIXTURE / "exams"),
                          "--exam-id", EXAM_ID, "--sheet-id", "s_cli", "--seed", "11")
        self.assertEqual(issued.returncode, 0, issued.stderr)
        self.assertEqual(json.loads(issued.stdout)["counts"]["presented_target"], 15)
        unfinished = self.cli("reveal", "--run-dir", str(self.run_dir), "--sheet-id", "s_cli")
        self.assertEqual(unfinished.returncode, 1)
        self.assertIn("Not finished", unfinished.stderr)
        process = subprocess.Popen([sys.executable, "-m", "evals.brainstorm", "serve", "--run-dir", str(self.run_dir), "--sheet-id", "s_cli"],
                                   cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.wait, timeout=10)
        self.addCleanup(process.kill)
        url = urlsplit(process.stdout.readline().strip())
        token = parse_qs(url.query)["t"][0]
        connection = http.client.HTTPConnection("127.0.0.1", url.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", f"/api/sheet?t={token}")
        state = json.loads(connection.getresponse().read())
        connection.close()
        body = json.dumps({"sheet_id": "s_cli", "answers": [{"item_id": item["item_id"], **ANSWER} for item in state["items"]]})
        connection.request("POST", f"/api/answers?t={token}", body=body.encode(), headers={
            "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{url.port}"})
        self.assertEqual(json.loads(connection.getresponse().read())["done"], 20)
        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=10), 0)
        revealed = self.cli("reveal", "--run-dir", str(self.run_dir), "--sheet-id", "s_cli")
        self.assertEqual(revealed.returncode, 0, revealed.stderr)
        self.assertEqual(json.loads(revealed.stdout)["verdicts"], 15)
        self.assertTrue((self.run_dir / EXAM_ID / "verdicts.jsonl").is_file())


class RepositoryRuleTests(unittest.TestCase):
    def test_entrypoint_adds_only_two_registrations(self):
        tree = ast.parse((ROOT / "evals/brainstorm/__main__.py").read_text())
        calls = [node.func.value.id for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "register"]
        self.assertEqual(calls, ["budget", "exam", "record", "dedupe", "loop", "metrics", "replay", "sheet", "serve",
                                 "report", "e2e"])

    def test_new_tests_never_build_a_real_backend(self):
        banned = ("Claude" + "CliBackend", "Command" + "Backend", '"kind": "' + "claude", '"kind": "' + "command")
        for name in ("test_sheet.py", "test_serve.py", "test_page_source.py"):
            source = (ROOT / "evals/brainstorm/tests" / name).read_text(encoding="utf-8")
            for word in banned:
                self.assertNotIn(word, source, name)

    def test_browser_checks_are_not_in_the_public_suite(self):
        for name in ("test_sheet.py", "test_serve.py", "test_page_source.py"):
            source = (ROOT / "evals/brainstorm/tests" / name).read_text(encoding="utf-8")
            self.assertNotIn("agent" + "-browser", source)
            self.assertNotIn("skip" + "Test", source)
