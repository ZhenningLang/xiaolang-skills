"""git_tidy.py: a worktree or branch is SAFE only when its code is already in trunk; apply re-checks each item.

Each case builds a bare origin and a working repo in a temporary directory and uses real git to simulate merges.
"""
import importlib.util
import json
import locale
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "git_tidy.py"
ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "me", "GIT_AUTHOR_EMAIL": "me@x", "GIT_COMMITTER_NAME": "me", "GIT_COMMITTER_EMAIL": "me@x",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd, *args, env=None):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env or ENV)
    if r.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fake_lsof(directory, stdout=b"", returncode=0, env_file=None):
    script = Path(directory) / "lsof"
    record_env = (f"import json, os\nfrom pathlib import Path\n"
                  f"Path({str(env_file)!r}).write_text(json.dumps(dict(os.environ)))\n") if env_file else ""
    write(script, f"#!{sys.executable}\nimport sys\n{record_env}"
                  f"sys.stdout.buffer.write({stdout!r})\nsys.exit({returncode})\n")
    script.chmod(0o755)
    return str(directory) + os.pathsep + ENV.get("PATH", "")


class ProcessCwdsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("git_tidy", SCRIPT)
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    def test_lsof_uses_utf8_locale_without_changing_parent_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "environment.json"
            path = fake_lsof(tmp, b"n/tmp/locale-cwd\n", env_file=env_file)
            with mock.patch.dict(os.environ, PATH=path, LC_ALL="C", GIT_TIDY_LOCALE_TEST="preserved"):
                before = dict(os.environ)
                self.assertEqual(self.module.process_cwds(), {os.path.realpath("/tmp/locale-cwd")})
                self.assertEqual(dict(os.environ), before)
            received = json.loads(env_file.read_text())
            self.assertEqual(received["LC_ALL"], "C.UTF-8")
            self.assertEqual(received["PATH"], path)
            self.assertEqual(received["GIT_TIDY_LOCALE_TEST"], "preserved")

    def test_real_lsof_finds_chinese_cwd_without_inherited_locale(self):
        real_lsof = shutil.which("lsof")
        if not real_lsof:
            self.skipTest("real lsof is not installed")
        previous_locale = locale.setlocale(locale.LC_CTYPE)
        try:
            locale.setlocale(locale.LC_CTYPE, "C.UTF-8")
        except locale.Error:
            self.skipTest("C.UTF-8 LC_CTYPE is unavailable")
        finally:
            locale.setlocale(locale.LC_CTYPE, previous_locale)
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve() / "中文目录"
            cwd.mkdir()
            proc = subprocess.Popen(["sleep", "60"], cwd=cwd)
            try:
                wrapper = Path(tmp) / "lsof"
                write(wrapper, f'#!/bin/sh\nexec {shlex.quote(real_lsof)} "$@" -p {proc.pid}\n')
                wrapper.chmod(0o755)
                env = {key: value for key, value in os.environ.items()
                       if key not in {"LANG", "LC_ALL", "LC_CTYPE"}}
                env["PATH"] = str(tmp) + os.pathsep + env.get("PATH", "")
                with mock.patch.dict(os.environ, env, clear=True):
                    cwds = self.module.process_cwds()
                self.assertIsNotNone(cwds)
                self.assertIn(str(cwd), cwds)
            finally:
                proc.kill()
                proc.wait(timeout=5)

    def test_lsof_escaped_cwd_makes_entire_result_unknown(self):
        cases = {
            "hex_escape": br"n/tmp/\xe4\xb8\xad" + b"\n",
            "escaped_backslash": br"n/tmp/a\\b" + b"\n",
            "control_escape": b"n/tmp/a^Ab\n",
            "single_backslash": br"n/tmp/a\b" + b"\n",
            "literal_caret": b"n/tmp/a^b\n",
            "normal_then_escaped": b"n/tmp/normal\n" + br"n/tmp/a\b" + b"\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, payload in cases.items():
                with self.subTest(name=name), mock.patch.dict(os.environ, PATH=fake_lsof(tmp, payload)):
                    self.assertIsNone(self.module.process_cwds())

    def test_lsof_escape_markers_in_other_fields_do_not_reject_cwds(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = br"p12\3^A" + b"\nn/tmp/normal\n"
            with mock.patch.dict(os.environ, PATH=fake_lsof(tmp, payload)):
                self.assertEqual(self.module.process_cwds(), {os.path.realpath("/tmp/normal")})

    def test_lsof_unicode_line_separators_preserve_complete_cwds(self):
        cases = {
            "line_separator": (b"n/tmp/a\xe2\x80\xa8b\n", "/tmp/a\u2028b"),
            "paragraph_separator_with_n_suffix": (b"n/tmp/a\xe2\x80\xa9nb\n", "/tmp/a\u2029nb"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, (payload, expected) in cases.items():
                with self.subTest(name=name), mock.patch.dict(os.environ, PATH=fake_lsof(tmp, payload)):
                    cwds = self.module.process_cwds()
                    self.assertEqual(cwds, {os.path.realpath(expected)})
                    self.assertTrue(self.module.under(expected, cwds))

    def test_lsof_malformed_fields_are_bounded_and_preserve_path_bytes(self):
        # No fuzz framework in this suite: cover truncated, misplaced, repeated and invalid-byte fields.
        cases = {
            "truncated_escape": (br"n/tmp/\x", None),
            "misplaced_markers": (b"p^\\nonsense\nn/tmp/normal\n", {os.path.realpath("/tmp/normal")}),
            "repeated_fields": (b"n/tmp/normal\n" * 1000, {os.path.realpath("/tmp/normal")}),
            "invalid_bytes": (b"n/tmp/raw-\x98\n", {os.path.realpath(os.fsdecode(b"/tmp/raw-\x98"))}),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, (payload, expected) in cases.items():
                with self.subTest(name=name), mock.patch.dict(os.environ, PATH=fake_lsof(tmp, payload)):
                    self.assertEqual(self.module.process_cwds(), expected)

    def test_non_utf8_lsof_cwd_preserves_path_bytes_and_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp).resolve() / "worktree"
            worktree.mkdir()
            raw_path = os.fsencode(Path(tmp).resolve()) + b"/cwd-\x98"
            payload = b"p123\nn" + raw_path + b"\nn" + os.fsencode(worktree) + b"\n"
            with mock.patch.dict(os.environ, PATH=fake_lsof(tmp, payload)):
                cwds = self.module.process_cwds()
            self.assertEqual({os.fsencode(cwd) for cwd in cwds}, {raw_path, os.fsencode(worktree)})
            self.assertIn(str(worktree), cwds)
            self.assertTrue(self.module.under(worktree, cwds))
            self.assertTrue(self.module.under(os.fsdecode(raw_path), cwds))

    def test_lsof_nonzero_without_output_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, PATH=fake_lsof(tmp, returncode=1)):
                self.assertIsNone(self.module.process_cwds())

    def test_lsof_nonzero_with_output_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = b"n" + os.fsencode(Path(tmp).resolve()) + b"\n"
            with mock.patch.dict(os.environ, PATH=fake_lsof(tmp, payload, returncode=1)):
                self.assertIsNone(self.module.process_cwds())

    def test_lsof_launch_oserror_is_unknown(self):
        with mock.patch.object(self.module.shutil, "which", return_value="/fake/lsof"), \
                mock.patch.object(self.module.subprocess, "run", side_effect=OSError("cannot start lsof")):
            self.assertIsNone(self.module.process_cwds())

    def test_lsof_timeout_is_unknown(self):
        with mock.patch.object(self.module.shutil, "which", return_value="/fake/lsof"), \
                mock.patch.object(self.module.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("lsof", 30)) as run:
            self.assertIsNone(self.module.process_cwds())
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertEqual(run.call_args.kwargs["encoding"], sys.getfilesystemencoding())
        self.assertEqual(run.call_args.kwargs["errors"], "surrogateescape")
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_successful_lsof_output_returns_realpaths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "cwd").mkdir()
            payload = b"p123\nn" + os.fsencode(root / "cwd" / "..") + b"\nn" + os.fsencode(root) + b"\n"
            with mock.patch.dict(os.environ, PATH=fake_lsof(tmp, payload)):
                self.assertEqual(self.module.process_cwds(), {str(root)})

    def test_successful_lsof_without_output_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, PATH=fake_lsof(tmp)):
                self.assertEqual(self.module.process_cwds(), set())

    def test_no_lsof_or_proc_is_unknown(self):
        with mock.patch.object(self.module.shutil, "which", return_value=None), \
                mock.patch.object(self.module, "Path") as path:
            path.return_value.is_dir.return_value = False
            self.assertIsNone(self.module.process_cwds())

    def test_no_lsof_uses_proc_and_skips_unreadable_cwds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with mock.patch.object(self.module.shutil, "which", return_value=None), \
                    mock.patch.object(self.module, "Path") as path, \
                    mock.patch.object(self.module.os, "readlink", side_effect=[str(root), PermissionError()]):
                path.return_value.is_dir.return_value = True
                path.return_value.iterdir.return_value = [Path("/proc/123"), Path("/proc/456"), Path("/proc/self")]
                self.assertEqual(self.module.process_cwds(), {str(root)})


class Repo:
    def __init__(self, tmp):
        self.tmp = Path(tmp).resolve()
        self.origin = self.tmp / "origin.git"
        self.path = self.tmp / "repo"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(self.tmp, "init", "-q", "-b", "main", str(self.path))
        git(self.path, "config", "user.email", "me@x")
        git(self.path, "config", "user.name", "me")
        git(self.path, "remote", "add", "origin", str(self.origin))
        write(self.path / "a.txt", "1\n2\n3\n")
        write(self.path / ".gitignore", "node_modules/\n.env\n")
        self.commit("init")
        git(self.path, "push", "-q", "-u", "origin", "main")
        git(self.path, "remote", "set-head", "origin", "main")

    def commit(self, msg, cwd=None, env=None):
        cwd = cwd or self.path
        git(cwd, "add", "-A")
        git(cwd, "commit", "-q", "-m", msg, env=env)
        return git(cwd, "rev-parse", "HEAD")

    def branch_with_change(self, name, filename, text):
        git(self.path, "switch", "-q", "-c", name, "main")
        write(self.path / filename, text)
        self.commit(f"{name} change")
        git(self.path, "switch", "-q", "main")

    def push_main(self):
        git(self.path, "push", "-q", "origin", "main")

    def run(self, *args, gh_prs=None):
        env = dict(ENV)
        fake_gh = self.tmp / "gh"
        payload = json.dumps([{"headRefName": n} for n in (gh_prs or [])])
        write(fake_gh, f"#!/bin/sh\necho '{payload}'\n")
        fake_gh.chmod(0o755)
        env["GIT_TIDY_GH"] = str(fake_gh)
        r = subprocess.run([sys.executable, str(SCRIPT), *args, "--repo", str(self.path)],
                           capture_output=True, text=True, env=env)
        return r

    def plan(self, *extra, gh_prs=None):
        out = self.tmp / "plan.json"
        r = self.run("plan", "--min-idle-hours", "0", "--plan-file", str(out), *extra, gh_prs=gh_prs)
        if r.returncode:
            raise AssertionError(r.stdout + r.stderr)
        return {(i["kind"], i["name"]): i for i in json.loads(out.read_text())["items"]}

    def apply(self, gh_prs=None):
        return self.run("apply", "--plan-file", str(self.tmp / "plan.json"), gh_prs=gh_prs)


class ContainmentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.r = Repo(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fast_forward_merged_branch_is_safe(self):
        self.r.branch_with_change("ff", "b.txt", "b\n")
        git(self.r.path, "merge", "-q", "--ff-only", "ff")
        self.r.push_main()
        item = self.r.plan()[("local", "ff")]
        self.assertEqual(item["verdict"], "SAFE", item)

    def test_squash_merged_branch_is_safe_although_not_ancestor(self):
        self.r.branch_with_change("sq", "b.txt", "b\n")
        git(self.r.path, "switch", "-q", "-c", "sq2", "sq")
        write(self.r.path / "c.txt", "c\n")
        self.r.commit("second")
        git(self.r.path, "switch", "-q", "main")
        git(self.r.path, "merge", "-q", "--squash", "sq2")
        self.r.commit("squash sq2")
        self.r.push_main()
        items = self.r.plan()
        self.assertEqual(items[("local", "sq2")]["verdict"], "SAFE", items[("local", "sq2")])

    def test_rebase_merged_branch_then_main_edits_same_lines_is_safe(self):
        # two commits cherry-picked one by one: the branch as one patch matches no single trunk commit; only the per-commit check sees it
        self.r.branch_with_change("rb", "a.txt", "1\nTWO\n3\n")
        git(self.r.path, "switch", "-q", "rb")
        write(self.r.path / "b.txt", "b\n")
        self.r.commit("rb second")
        git(self.r.path, "switch", "-q", "main")
        git(self.r.path, "cherry-pick", "rb~1", "rb")
        write(self.r.path / "a.txt", "1\nzwei\n3\n")
        self.r.commit("main rewrites same line later")
        self.r.push_main()
        item = self.r.plan()[("local", "rb")]
        self.assertEqual(item["verdict"], "SAFE", item)

    def test_squash_merged_then_main_edits_same_lines_is_safe(self):
        self.r.branch_with_change("sq", "a.txt", "1\nTWO\n3\n")
        git(self.r.path, "switch", "-q", "sq")
        write(self.r.path / "b.txt", "b\n")
        self.r.commit("second")
        git(self.r.path, "switch", "-q", "main")
        git(self.r.path, "merge", "-q", "--squash", "sq")
        self.r.commit("squash sq")
        write(self.r.path / "a.txt", "1\nzwei\n3\n")
        self.r.commit("later edit")
        self.r.push_main()
        item = self.r.plan()[("local", "sq")]
        self.assertEqual(item["verdict"], "SAFE", item)

    def test_branch_with_partial_squash_is_kept(self):
        self.r.branch_with_change("part", "b.txt", "b\n")
        git(self.r.path, "switch", "-q", "part")
        write(self.r.path / "c.txt", "c\n")
        self.r.commit("not merged")
        git(self.r.path, "switch", "-q", "main")
        write(self.r.path / "b.txt", "b\n")
        self.r.commit("only half of part")
        self.r.push_main()
        item = self.r.plan()[("local", "part")]
        self.assertEqual(item["verdict"], "KEEP", item)

    def test_unmerged_branch_is_kept(self):
        self.r.branch_with_change("wip", "b.txt", "b\n")
        item = self.r.plan()[("local", "wip")]
        self.assertEqual(item["verdict"], "KEEP")
        self.assertIn("not in trunk", " ".join(item["reasons"]))

    def test_merge_only_in_local_main_is_not_enough(self):
        self.r.branch_with_change("local-only", "b.txt", "b\n")
        git(self.r.path, "merge", "-q", "--ff-only", "local-only")
        item = self.r.plan()[("local", "local-only")]
        self.assertEqual(item["verdict"], "KEEP", "trunk is origin/main; an unpushed local main does not count")

    def test_protected_names_are_never_listed(self):
        git(self.r.path, "branch", "develop", "main")
        git(self.r.path, "branch", "release/1.0", "main")
        items = self.r.plan()
        self.assertNotIn(("local", "develop"), items)
        self.assertNotIn(("local", "release/1.0"), items)
        self.assertNotIn(("local", "main"), items)


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.r = Repo(self._tmp.name)
        self.wt = self.r.tmp / "wt-feat"
        git(self.r.path, "worktree", "add", "-q", str(self.wt), "-b", "feat", "main")
        write(self.wt / "b.txt", "b\n")
        self.r.commit("feat", cwd=self.wt)
        git(self.r.path, "merge", "-q", "--squash", "feat")
        self.r.commit("squash feat")
        self.r.push_main()

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_merged_worktree_is_safe_and_apply_removes_it_and_branch(self):
        write(self.wt / "node_modules" / "x.js", "x")
        item = self.r.plan()[("worktree", str(self.wt))]
        self.assertEqual(item["verdict"], "SAFE", item)
        r = self.r.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.wt.exists())
        self.assertEqual(git(self.r.path, "branch", "--list", "feat"), "")
        restore = list((self.r.path / ".git" / "git-tidy").glob("restore-*.json"))
        self.assertEqual(len(restore), 1)
        self.assertIn("feat", restore[0].read_text())

    def test_apply_ends_with_summary_of_deleted_and_kept_items(self):
        self.r.branch_with_change("wip", "c.txt", "c\n")
        items = self.r.plan()
        self.assertEqual(items[("local", "wip")]["verdict"], "KEEP")
        r = self.r.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        summary = r.stdout[r.stdout.index("== Summary =="):]
        self.assertIn("deleted 1, skipped after re-check 0, failed 0", summary)
        self.assertIn(f"deleted worktree (1): {self.wt} [feat]", summary)
        self.assertIn("still left: 1", summary)
        self.assertIn("has commits not in trunk 1: wip", summary)
        self.assertIn("restore record: ", summary)

    def test_untracked_file_keeps_worktree_and_branch(self):
        write(self.wt / "notes.md", "draft")
        items = self.r.plan()
        self.assertEqual(items[("worktree", str(self.wt))]["verdict"], "KEEP")
        self.assertNotIn(("local", "feat"), items, "a branch checked out in a worktree goes with the worktree, not listed alone")

    def test_modified_tracked_file_keeps_worktree(self):
        write(self.wt / "a.txt", "changed\n")
        self.assertEqual(self.r.plan()[("worktree", str(self.wt))]["verdict"], "KEEP")

    def test_ignored_non_cache_file_keeps_worktree(self):
        write(self.wt / ".env", "SECRET=1")
        item = self.r.plan()[("worktree", str(self.wt))]
        self.assertEqual(item["verdict"], "KEEP")
        self.assertIn(".env", " ".join(item["reasons"]))

    def test_disposable_patterns_from_repo_config_and_flag_unblock_ignored_files(self):
        write(self.wt / ".env", "SECRET=1")
        git(self.r.path, "config", "--add", "git-tidy.disposable", ".env")
        self.assertEqual(self.r.plan()[("worktree", str(self.wt))]["verdict"], "SAFE")
        git(self.r.path, "config", "--unset-all", "git-tidy.disposable")
        self.assertEqual(self.r.plan()[("worktree", str(self.wt))]["codes"], ["ignored"])
        self.assertEqual(self.r.plan("--disposable", ".e*")[("worktree", str(self.wt))]["verdict"], "SAFE")

    def test_disposable_pattern_with_trailing_slash_matches_ignored_directory(self):
        # `git config git-tidy.disposable '.health-cache/'` is how people write a directory;
        # it used to be compared against the bare name `.health-cache` and silently never matched.
        exclude = Path(git(self.r.path, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info/exclude"
        write(exclude, ".health-cache/\n")
        write(self.wt / ".health-cache/k.json", "{}")
        self.assertEqual(self.r.plan()[("worktree", str(self.wt))]["codes"], ["ignored"])
        git(self.r.path, "config", "--add", "git-tidy.disposable", ".health-cache/")
        self.assertEqual(self.r.plan()[("worktree", str(self.wt))]["verdict"], "SAFE")
        git(self.r.path, "config", "--unset-all", "git-tidy.disposable")
        self.assertEqual(self.r.plan("--disposable", "./.health-cache/")[("worktree", str(self.wt))]["verdict"], "SAFE")

    def salvage_root(self):
        return Path(git(self.r.path, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "git-tidy/salvage"

    def test_salvage_pattern_copies_ignored_file_out_then_removes_worktree(self):
        write(self.wt / ".env", "SECRET=new")
        git(self.r.path, "config", "--add", "git-tidy.salvage", ".env")
        item = self.r.plan()[("worktree", str(self.wt))]
        self.assertEqual(item["verdict"], "SAFE", item)
        self.assertEqual(item["salvage"], [".env"])
        r = self.r.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.wt.exists())
        copies = list(self.salvage_root().glob("*/wt-feat/.env"))
        self.assertEqual([c.read_text() for c in copies], ["SECRET=new"])
        self.assertIn("copied out first", r.stdout[r.stdout.index("== Summary =="):])
        restore = json.loads(next((self.r.path / ".git" / "git-tidy").glob("restore-*.json")).read_text())
        self.assertEqual(restore["items"][0]["salvaged"], {".env": str(copies[0])})

    def test_salvage_skips_copy_when_main_checkout_has_identical_content(self):
        write(self.r.path / ".env", "SAME")
        write(self.wt / ".env", "SAME")
        r = self.r.plan("--salvage", ".env")
        r = self.r.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.wt.exists())
        self.assertEqual(list(self.salvage_root().rglob(".env")), [])
        restore = json.loads(next((self.r.path / ".git" / "git-tidy").glob("restore-*.json")).read_text())
        self.assertIn("main checkout", restore["items"][0]["salvaged"][".env"])

    def test_salvage_directory_keeps_nested_files_and_disposable_still_drops(self):
        exclude = Path(git(self.r.path, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info/exclude"
        write(exclude, ".agent-runs/\n.agent-state/\n")
        write(self.r.path / ".agent-runs/run1/RESUME.md", "different")
        write(self.wt / ".agent-runs/run1/RESUME.md", "resume")
        write(self.wt / ".agent-runs/run1/sub/trace.md", "trace")
        write(self.wt / ".agent-state/hook", "x")
        self.assertEqual(self.r.plan("--salvage", "./.agent-runs/")[("worktree", str(self.wt))]["codes"], ["ignored"])
        item = self.r.plan("--salvage", "./.agent-runs/", "--disposable", ".agent-state")[("worktree", str(self.wt))]
        self.assertEqual(item["verdict"], "SAFE", item)
        r = self.r.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [run] = list(self.salvage_root().glob("*/wt-feat/.agent-runs"))
        self.assertEqual((run / "run1/RESUME.md").read_text(), "resume")
        self.assertEqual((run / "run1/sub/trace.md").read_text(), "trace")
        self.assertEqual(list(self.salvage_root().rglob(".agent-state")), [])

    def test_patterns_inside_a_wholly_ignored_directory_are_matched(self):
        # git ls-files --directory folds the whole ignored notes/ into one line; a sub-path pattern like notes/.local must still match
        exclude = Path(git(self.r.path, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info/exclude"
        write(exclude, "notes/\n")
        write(self.wt / "notes/.local/throttle.lock", "")
        write(self.wt / "notes/.inbox/raw/x.json", "{}")
        item = self.r.plan("--salvage", "notes/.inbox")[("worktree", str(self.wt))]
        self.assertEqual(item["ignored"], ["notes/.local/"])
        self.assertEqual(item["salvage"], ["notes/.inbox"])
        item = self.r.plan("--salvage", "notes/.inbox", "--disposable", "notes/.local")[("worktree", str(self.wt))]
        self.assertEqual(item["verdict"], "SAFE", item)
        item = self.r.plan("--disposable", "notes/*")[("worktree", str(self.wt))]
        self.assertEqual(item["verdict"], "SAFE", item)

    def test_salvage_directory_listed_together_with_its_ignored_children_copies_once(self):
        # when notes/ holds only ignored subdirectories, ls-files lists notes/, notes/.local/ and notes/.inbox/;
        # all three match salvage=notes; it used to copy notes and then notes/.local, and the second copy's File exists kept the worktree
        exclude = Path(git(self.r.path, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info/exclude"
        write(exclude, "/notes/.local/\n/notes/.inbox/\n")
        write(self.wt / "notes/.local/throttle.lock", "lock")
        write(self.wt / "notes/.inbox/raw/x.json", "{}")
        item = self.r.plan("--salvage", "notes")[("worktree", str(self.wt))]
        self.assertEqual(item["verdict"], "SAFE", item)
        self.assertEqual(item["salvage"], ["notes"])
        r = self.r.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.wt.exists())
        [mem] = list(self.salvage_root().glob("*/wt-feat/notes"))
        self.assertEqual((mem / ".local/throttle.lock").read_text(), "lock")
        self.assertEqual((mem / ".inbox/raw/x.json").read_text(), "{}")

    def test_salvage_failure_keeps_worktree(self):
        write(self.wt / ".env", "SECRET=new")
        self.r.plan("--salvage", ".env")
        root = self.salvage_root()
        write(root, "not a directory")  # the copy-out directory cannot be created
        r = self.r.apply()
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.wt / ".env").exists())
        self.assertNotEqual(git(self.r.path, "branch", "--list", "feat"), "")
        self.assertIn("copy-out failed", r.stdout)

    def test_apply_refuses_options_that_differ_from_the_plan_file(self):
        # apply reuses the plan file's options; a flag that disagrees means the plan file was
        # overwritten by another plan run, so deleting from it would not match what was asked.
        self.assertEqual(self.r.plan()[("worktree", str(self.wt))]["verdict"], "SAFE")
        r = self.r.run("apply", "--plan-file", str(self.r.tmp / "plan.json"), "--min-idle-hours", "12")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("plan --min-idle-hours 12", r.stdout + r.stderr)
        self.assertTrue(self.wt.exists())
        r = self.r.run("apply", "--plan-file", str(self.r.tmp / "plan.json"), "--min-idle-hours", "0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("min_idle_hours=0", r.stdout)
        self.assertFalse(self.wt.exists())

    def test_process_inside_worktree_keeps_it(self):
        proc = subprocess.Popen(["sleep", "30"], cwd=self.wt)
        try:
            item = self.r.plan()[("worktree", str(self.wt))]
        finally:
            proc.kill()
            proc.wait()
        self.assertEqual(item["verdict"], "KEEP")
        self.assertIn("process", " ".join(item["reasons"]))

    def test_lsof_failure_keeps_otherwise_deletable_worktree_in_plan(self):
        with mock.patch.dict(ENV, PATH=fake_lsof(self.r.tmp)):
            item = self.r.plan()[("worktree", str(self.wt))]
        self.assertEqual(item["verdict"], "SAFE", item)
        with mock.patch.dict(ENV, PATH=fake_lsof(self.r.tmp, returncode=1)):
            result = self.r.run("plan", "--min-idle-hours", "0", "--plan-file", str(self.r.tmp / "plan.json"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        items = json.loads((self.r.tmp / "plan.json").read_text())["items"]
        item = next(i for i in items if i["kind"] == "worktree" and i["name"] == str(self.wt))
        self.assertEqual(item["verdict"], "KEEP", item)
        self.assertEqual(item["codes"], ["in_use"])
        self.assertIn("cannot confirm", " ".join(item["reasons"]))
        self.assertNotIn(str(self.wt), result.stdout.split("== Kept ==")[0])
        self.assertTrue(self.wt.exists())

    def test_escaped_lsof_output_keeps_otherwise_deletable_worktree(self):
        with mock.patch.dict(ENV, PATH=fake_lsof(self.r.tmp)):
            self.assertEqual(self.r.plan()[("worktree", str(self.wt))]["verdict"], "SAFE")
        payload = b"n/tmp/normal\n" + br"n/tmp/\xe4\xb8\xad" + b"\n"
        with mock.patch.dict(ENV, PATH=fake_lsof(self.r.tmp, payload)):
            item = self.r.plan()[("worktree", str(self.wt))]
        self.assertEqual(item["verdict"], "KEEP", item)
        self.assertEqual(item["codes"], ["in_use"])
        self.assertEqual(item["reasons"], ["cannot confirm whether a process uses it (lsof missing, failed, or its output had escapes)"])
        self.assertTrue(self.wt.exists())

    def test_recent_activity_keeps_worktree(self):
        out = self.r.tmp / "plan.json"
        r = self.r.run("plan", "--min-idle-hours", "6", "--plan-file", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        item = {(i["kind"], i["name"]): i for i in json.loads(out.read_text())["items"]}[("worktree", str(self.wt))]
        self.assertEqual(item["verdict"], "KEEP")

    def test_locked_worktree_is_kept(self):
        git(self.r.path, "worktree", "lock", str(self.wt))
        self.assertEqual(self.r.plan()[("worktree", str(self.wt))]["verdict"], "KEEP")

    def test_apply_rechecks_and_skips_item_that_changed_after_plan(self):
        self.assertEqual(self.r.plan()[("worktree", str(self.wt))]["verdict"], "SAFE")
        write(self.wt / "late.md", "written after plan")
        r = self.r.apply()
        self.assertTrue(self.wt.exists(), r.stdout)
        self.assertIn("re-check", r.stdout)
        self.assertEqual(git(self.r.path, "branch", "--list", "feat").strip("*+ "), "feat")

    def test_missing_worktree_dir_is_pruned(self):
        subprocess.run(["rm", "-rf", str(self.wt)], check=True)
        items = self.r.plan()
        self.assertEqual(items[("prune", str(self.wt))]["verdict"], "SAFE")


class SubmoduleTest(unittest.TestCase):
    """With initialised submodules git refuses to remove a worktree without --force; force only when the submodules are clean."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.r = Repo(self._tmp.name)
        self.env = {**ENV, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow",
                    "GIT_CONFIG_VALUE_0": "always"}
        sub = self.r.tmp / "sub"
        git(self.r.tmp, "init", "-q", "-b", "main", str(sub))
        write(sub / "s.txt", "s\n")
        write(sub / ".gitignore", ".env\n")
        self.r.commit("sub init", cwd=sub)
        git(self.r.path, "submodule", "add", "-q", str(sub), "deps/sub", env=self.env)
        self.r.commit("add submodule")
        self.r.push_main()
        self.wt = self.r.tmp / "wt-sub"
        git(self.r.path, "worktree", "add", "-q", str(self.wt), "-b", "feat-sub", "main")
        git(self.wt, "submodule", "update", "-q", "--init", env=self.env)
        write(self.wt / "b.txt", "b\n")
        self.r.commit("feat-sub", cwd=self.wt)
        git(self.r.path, "merge", "-q", "--squash", "feat-sub")
        self.r.commit("squash feat-sub")
        self.r.push_main()

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_submodule_worktree_is_removed(self):
        self.assertEqual(self.r.plan()[("worktree", str(self.wt))]["verdict"], "SAFE")
        r = self.r.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.wt.exists())

    def test_untracked_file_inside_submodule_keeps_worktree(self):
        write(self.wt / "deps" / "sub" / "draft.md", "x")
        self.assertEqual(self.r.plan()[("worktree", str(self.wt))]["verdict"], "KEEP")

    def test_ignored_file_inside_submodule_keeps_worktree(self):
        write(self.wt / "deps" / "sub" / ".env", "SECRET=1")
        item = self.r.plan()[("worktree", str(self.wt))]
        self.assertEqual(item["verdict"], "KEEP")
        self.assertIn("deps/sub/.env", " ".join(item["reasons"]))

    def test_unrecorded_commit_inside_submodule_keeps_worktree(self):
        write(self.wt / "deps" / "sub" / "n.txt", "n\n")
        self.r.commit("local sub commit", cwd=self.wt / "deps" / "sub")
        self.assertEqual(self.r.plan()[("worktree", str(self.wt))]["verdict"], "KEEP")


class RemoteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.r = Repo(self._tmp.name)
        self.r.branch_with_change("done", "b.txt", "b\n")
        git(self.r.path, "push", "-q", "origin", "done")
        git(self.r.path, "merge", "-q", "--squash", "done")
        self.r.commit("squash done")
        self.r.push_main()
        git(self.r.path, "branch", "-D", "done")

    def tearDown(self):
        self._tmp.cleanup()

    def test_merged_remote_branch_is_deleted_with_lease(self):
        self.assertEqual(self.r.plan()[("remote", "origin/done")]["verdict"], "SAFE")
        r = self.r.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(git(self.r.origin, "branch", "--list", "done"), "")

    def test_several_remote_branches_deleted_in_one_push_except_kept_ones(self):
        for name in ("done2", "wip"):
            self.r.branch_with_change(name, f"{name}.txt", f"{name}\n")
            git(self.r.path, "push", "-q", "origin", name)
        git(self.r.path, "merge", "-q", "--ff-only", "done2")
        self.r.push_main()
        self.r.plan()
        r = self.r.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        left = git(self.r.origin, "branch", "--format=%(refname:short)").split()
        self.assertEqual(sorted(left), ["main", "wip"])

    def test_open_pr_keeps_remote_branch(self):
        item = self.r.plan(gh_prs=["done"])[("remote", "origin/done")]
        self.assertEqual(item["verdict"], "KEEP")
        self.assertIn("PR", " ".join(item["reasons"]))

    def test_other_authors_branch_is_kept_unless_included(self):
        other = {**ENV, "GIT_AUTHOR_EMAIL": "other@x", "GIT_COMMITTER_EMAIL": "other@x"}
        git(self.r.path, "switch", "-q", "-c", "theirs", "main")
        write(self.r.path / "t.txt", "t\n")
        self.r.commit("theirs", env=other)
        git(self.r.path, "push", "-q", "origin", "theirs")
        git(self.r.path, "switch", "-q", "main")
        git(self.r.path, "merge", "-q", "--ff-only", "theirs")
        self.r.push_main()
        self.assertEqual(self.r.plan()[("remote", "origin/theirs")]["verdict"], "KEEP")
        self.assertEqual(self.r.plan("--include-others")[("remote", "origin/theirs")]["verdict"], "SAFE")

    def test_remote_moved_after_plan_is_not_deleted(self):
        self.r.plan()
        other = self.r.tmp / "clone"
        git(self.r.tmp, "clone", "-q", str(self.r.origin), str(other))
        git(other, "switch", "-q", "done")
        write(other / "new.txt", "new\n")
        self.r.commit("someone pushed more", cwd=other)
        git(other, "push", "-q", "origin", "done")
        self.r.apply()
        self.assertNotEqual(git(self.r.origin, "branch", "--list", "done"), "")

    def test_gh_failure_keeps_remote_branches(self):
        env = dict(ENV, GIT_TIDY_GH="/nonexistent/gh")
        out = self.r.tmp / "plan.json"
        r = subprocess.run([sys.executable, str(SCRIPT), "plan", "--min-idle-hours", "0", "--plan-file", str(out),
                            "--repo", str(self.r.path)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        items = {(i["kind"], i["name"]): i for i in json.loads(out.read_text())["items"]}
        self.assertEqual(items[("remote", "origin/done")]["verdict"], "KEEP")


if __name__ == "__main__":
    unittest.main()
