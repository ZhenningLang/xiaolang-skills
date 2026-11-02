import ast
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from evals.brainstorm.budget import default_ledger_path


ROOT = Path(__file__).resolve().parents[3]


class ContractTests(unittest.TestCase):
    def test_verification_gate_rejects_skipped_and_undiscovered_tests(self):
        from evals.brainstorm.tests.__main__ import run
        def skipped():
            raise unittest.SkipTest("synthetic gate control")
        for suite, code in ((unittest.TestSuite([unittest.FunctionTestCase(skipped)]), 1),
                            (unittest.TestSuite(), 1),
                            (unittest.TestSuite([unittest.FunctionTestCase(lambda: None)]), 0)):
            with patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(run(suite), code)

    def test_private_and_run_paths_ignored_but_samples_public(self):
        for name, expected in (("evals/brainstorm/exams/private/question.json", 0),
                               ("evals/brainstorm/runs/batch/ideas.jsonl", 0),
                               ("evals/brainstorm/exams/samples/question.json", 1)):
            result = subprocess.run(["git", "check-ignore", "--no-index", "--quiet", name],
                                    cwd=ROOT, timeout=10)
            self.assertEqual(result.returncode, expected, name)

    def test_only_standard_library_and_self_imports(self):
        for path in (ROOT / "evals").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                for name in names:
                    self.assertIn(name, sys.stdlib_module_names | {"evals"}, str(path))

    def test_real_default_path_does_not_follow_run_directory(self):
        expected = ROOT / "evals" / "brainstorm" / "runs" / "ledger.real.jsonl"
        self.assertEqual(default_ledger_path("claude", "one"), expected)
        self.assertEqual(default_ledger_path("claude", "two"), expected)

    def test_entrypoint_only_registers_and_dispatches(self):
        tree = ast.parse((ROOT / "evals/brainstorm/__main__.py").read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertTrue(any(isinstance(node.func, ast.Attribute) and node.func.attr == "register" for node in calls))
        self.assertFalse(any(isinstance(node, ast.ClassDef) for node in ast.walk(tree)))
        self.assertFalse(any(isinstance(node, (ast.For, ast.While, ast.With, ast.Try)) for node in ast.walk(tree)))
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertNotIn("decimal", imports)

    def test_readme_documents_cost_boundaries(self):
        text = (ROOT / "evals/README.md").read_text().lower()
        for concept in ("estimated", "exit code 3", "single call", "output", "unverified",
                        "personal", "pricing", "fixed_tokens", "posix", "confirm-not-charged"):
            self.assertIn(concept, text)
