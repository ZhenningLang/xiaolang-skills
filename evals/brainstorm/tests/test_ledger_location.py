"""The real ledger lives outside any worktree, and an old one is never silently left behind."""

import argparse
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evals.brainstorm import budget
from evals.brainstorm.budget import BudgetLedger, LedgerError, default_ledger_path, legacy_ledger_path, real_ledger_path


ROOT = Path(__file__).resolve().parents[3]
SUFFIX = Path("xiaolang-skills/brainstorm/ledger.real.jsonl")


class Sandbox(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name).resolve()
        self.home = self.temp / "home"
        self.state = self.temp / "state"
        self.repo = self.temp / "repo"
        patcher = patch.dict(os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def plant_old_ledger(self):
        old = legacy_ledger_path(repo_root=self.repo)
        old.parent.mkdir(parents=True)
        old.write_text("", encoding="utf-8")
        return old


class IsolationTests(unittest.TestCase):
    def test_the_whole_suite_runs_under_a_throwaway_home(self):
        marker = os.environ.get("BRAINSTORM_TESTS_HOME")
        self.assertTrue(marker)
        self.assertEqual(os.environ["HOME"], marker)
        self.assertTrue(real_ledger_path().is_relative_to(marker))
        self.assertTrue(Path.home().is_relative_to(marker))


class PathTests(Sandbox):
    def test_state_home_decides_the_location(self):
        self.assertEqual(real_ledger_path(), self.state / SUFFIX)

    def test_unset_state_home_falls_back_to_the_home_directory(self):
        del os.environ["XDG_STATE_HOME"]
        self.assertEqual(real_ledger_path(), self.home / ".local/state" / SUFFIX)

    def test_relative_state_home_is_ignored_as_the_spec_says(self):
        os.environ["XDG_STATE_HOME"] = "relative/state"
        self.assertEqual(real_ledger_path(), self.home / ".local/state" / SUFFIX)

    def test_real_default_is_outside_the_repository_and_ignores_the_run_directory(self):
        first = default_ledger_path("claude", "one", repo_root=self.repo)
        self.assertEqual(first, self.state / SUFFIX)
        self.assertEqual(default_ledger_path("command", "two", repo_root=self.repo), first)
        self.assertFalse(first.is_relative_to(self.repo))

    def test_fake_default_stays_in_the_run_directory(self):
        self.assertEqual(default_ledger_path("fake", self.temp / "run"), self.temp / "run/ledger.fake.jsonl")

    def test_lock_and_cache_sit_next_to_the_new_ledger(self):
        ledger = BudgetLedger(default_ledger_path("claude", "one", repo_root=self.repo))
        self.assertEqual(ledger.lock_path.parent, (self.state / SUFFIX).parent)
        self.assertEqual(ledger.cache_dir.parent, (self.state / SUFFIX).parent)


class SplitLedgerTests(Sandbox):
    def test_old_ledger_without_new_one_refuses_and_says_how_to_move_it(self):
        old = self.plant_old_ledger()
        with self.assertRaises(LedgerError) as caught:
            default_ledger_path("claude", "one", repo_root=self.repo)
        message = str(caught.exception)
        self.assertIn(str(old), message)
        self.assertIn(str(self.state / SUFFIX), message)
        self.assertIn(".cache", message)
        self.assertIn("mv", message)

    def test_refusal_moves_nothing_and_creates_nothing(self):
        old = self.plant_old_ledger()
        self.assertRaises(LedgerError, default_ledger_path, "claude", "one", repo_root=self.repo)
        self.assertTrue(old.exists())
        self.assertFalse(self.state.exists())

    def test_both_paths_present_uses_the_new_one(self):
        self.plant_old_ledger()
        new = self.state / SUFFIX
        new.parent.mkdir(parents=True)
        new.write_text("", encoding="utf-8")
        self.assertEqual(default_ledger_path("claude", "one", repo_root=self.repo), new)

    def test_no_old_ledger_starts_clean(self):
        self.assertEqual(default_ledger_path("claude", "one", repo_root=self.repo), self.state / SUFFIX)
        self.assertFalse(self.state.exists())

    def test_fake_backend_never_looks_at_the_old_ledger(self):
        self.plant_old_ledger()
        with patch.object(budget, "legacy_ledger_path", side_effect=AssertionError("fake must not check")):
            self.assertEqual(default_ledger_path("fake", self.temp), self.temp / "ledger.fake.jsonl")


class CommandTests(Sandbox):
    def test_commands_start_without_touching_the_real_default(self):
        # Registering the parser must not evaluate the real default: a fake run has to work beside an old ledger.
        done = subprocess.run([sys.executable, "-m", "evals.brainstorm", "--help"], cwd=ROOT, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.state.exists())

    def test_ledger_command_default_follows_the_new_location(self):
        args = argparse.Namespace(ledger=None, limit="50", action="status")
        with (patch.object(budget, "legacy_ledger_path", return_value=self.repo / "absent"), contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(budget.cli_boundary(budget._run, args), 0)
        self.assertTrue((self.state / SUFFIX).with_name(SUFFIX.name + ".lock").exists())

    def test_ledger_command_default_refuses_beside_an_old_ledger(self):
        args = argparse.Namespace(ledger=None, limit="50", action="status")
        with (patch.object(budget, "legacy_ledger_path", return_value=self.plant_old_ledger()), contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(budget.cli_boundary(budget._run, args), 1)
        self.assertFalse(self.state.exists())

    def test_explicit_ledger_path_still_wins_beside_an_old_ledger(self):
        self.plant_old_ledger()
        explicit = self.temp / "chosen/ledger.jsonl"
        self.assertEqual(BudgetLedger(explicit).status()["used"], 0)
        self.assertEqual(BudgetLedger(explicit).path, explicit)


class ReadmeTests(unittest.TestCase):
    def test_readme_names_the_new_location_and_the_move(self):
        text = (ROOT / "evals/README.md").read_text(encoding="utf-8")
        self.assertIn("XDG_STATE_HOME", text)
        self.assertIn("~/.local/state/xiaolang-skills/brainstorm/ledger.real.jsonl", text)
        self.assertNotIn("selects `run_dir/ledger.fake.jsonl` for\nfake calls and `evals/brainstorm/runs/ledger.real.jsonl`", text)


if __name__ == "__main__":
    unittest.main()
