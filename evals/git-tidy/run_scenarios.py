"""Scenario comparison: git-tidy's verdicts against the usual `git branch --merged` cleanup.

Each scenario builds a throwaway repository with a bare origin, in a state whose right answer is known
from how it was built: either everything on the item is already in trunk and nothing would be lost
(should delete), or deleting it would lose work or break something (should keep).

Two ways of picking what to delete are asked about every scenario:

- git-tidy: `plan --no-fetch --min-idle-hours 0` (the recency rule is switched off, since every scenario
  was just created; a fake `gh` reports open pull requests).
- the usual one-liner: a local branch, or a worktree's branch, listed by `git branch --merged` in the main
  checkout, and a remote branch listed by `git branch -r --merged`, excluding main and master. This is
  the selection step of `git branch --merged | grep -vE 'main|master' | xargs git branch -d` and its
  worktree and remote variants.

Writes evals/git-tidy/results/<date>-scenarios.json. Standard library and git only.

    python3 evals/git-tidy/run_scenarios.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "git-tidy" / "scripts" / "git_tidy.py"
RESULTS = Path(__file__).resolve().parent / "results"
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
       "GIT_AUTHOR_NAME": "me", "GIT_AUTHOR_EMAIL": "me@x", "GIT_COMMITTER_NAME": "me", "GIT_COMMITTER_EMAIL": "me@x",
       "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=ENV, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


class Repo:
    """A bare origin and a main checkout with one pushed commit on main."""

    def __init__(self, tmp: Path):
        self.origin, self.path = tmp / "origin.git", tmp / "repo"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(tmp, "clone", "-q", str(self.origin), str(self.path))
        self.commit("base.txt", "base\n", "base")
        git(self.path, "push", "-q", "origin", "main")
        git(self.path, "remote", "set-head", "origin", "main")

    def commit(self, name, text, msg, cwd=None):
        cwd = cwd or self.path
        Path(cwd, name).write_text(text)
        git(cwd, "add", name)
        git(cwd, "commit", "-q", "-m", msg)

    def branch_with(self, branch, *files):
        git(self.path, "switch", "-q", "-c", branch)
        for n, name in enumerate(files):
            self.commit(name, f"{branch} {n}\n", f"{branch} {name}")
        git(self.path, "switch", "-q", "main")

    def push_main(self):
        git(self.path, "push", "-q", "origin", "main")


# Each scenario returns (kind, name) of the item under test. `truth` is the right answer.
def merge_commit(r):
    r.branch_with("feat", "a.txt")
    git(r.path, "merge", "-q", "--no-ff", "-m", "merge feat", "feat")
    r.push_main()
    return "local", "feat"


def fast_forward(r):
    r.branch_with("feat", "a.txt")
    git(r.path, "merge", "-q", "--ff-only", "feat")
    r.push_main()
    return "local", "feat"


def squash_merge(r):
    r.branch_with("feat", "a.txt", "b.txt")
    git(r.path, "merge", "-q", "--squash", "feat")
    git(r.path, "commit", "-q", "-m", "feat (squashed)")
    r.push_main()
    return "local", "feat"


def rebase_merge(r):
    r.commit("other.txt", "other\n", "someone else's change")
    r.push_main()
    git(r.path, "switch", "-q", "-c", "feat", "HEAD~1")
    r.commit("a.txt", "a\n", "feat a")
    r.commit("b.txt", "b\n", "feat b")
    git(r.path, "switch", "-q", "main")
    git(r.path, "cherry-pick", "main..feat")
    r.push_main()
    return "local", "feat"


def unmerged(r):
    r.branch_with("feat", "a.txt")
    return "local", "feat"


def merged_then_more(r):
    r.branch_with("feat", "a.txt")
    git(r.path, "merge", "-q", "--no-ff", "-m", "merge feat", "feat")
    r.push_main()
    git(r.path, "switch", "-q", "feat")
    r.commit("later.txt", "later\n", "work after the merge")
    git(r.path, "switch", "-q", "main")
    return "local", "feat"


def merged_worktree(r, setup):
    r.branch_with("feat", "a.txt")
    git(r.path, "merge", "-q", "--no-ff", "-m", "merge feat", "feat")
    r.push_main()
    wt = r.path.parent / "wt-feat"
    git(r.path, "worktree", "add", "-q", str(wt), "feat")
    setup(wt)
    return "worktree", str(wt)


def worktree_dirty(r):
    return merged_worktree(r, lambda wt: Path(wt, "a.txt").write_text("edited, not committed\n"))


def worktree_untracked(r):
    return merged_worktree(r, lambda wt: Path(wt, "notes.txt").write_text("new file\n"))


def worktree_ignored_env(r):
    def setup(wt):
        Path(wt, ".gitignore").write_text(".env\n")
        git(wt, "add", ".gitignore")
        git(wt, "commit", "-q", "-m", "ignore .env")
        git(r.path, "merge", "-q", "--no-ff", "-m", "merge ignore", "feat")
        r.push_main()
        Path(wt, ".env").write_text("TOKEN=only-copy\n")
    return merged_worktree(r, setup)


def protected_branch(r):
    git(r.path, "branch", "develop")
    git(r.path, "push", "-q", "origin", "develop")
    return "local", "develop"


def remote_open_pr(r):
    r.branch_with("feat", "a.txt")
    git(r.path, "push", "-q", "origin", "feat")
    git(r.path, "merge", "-q", "--ff-only", "feat")
    r.push_main()
    git(r.path, "branch", "-D", "feat")
    return "remote", "origin/feat"


def local_main_only(r):
    r.branch_with("feat", "a.txt")
    git(r.path, "merge", "-q", "--no-ff", "-m", "merge feat", "feat")
    return "local", "feat"


SCENARIOS = [
    ("merge-commit", merge_commit, "delete", "merged with a merge commit and pushed"),
    ("fast-forward", fast_forward, "delete", "fast-forwarded into main and pushed"),
    ("squash-merge", squash_merge, "delete", "two commits squashed into one on main"),
    ("rebase-merge", rebase_merge, "delete", "each commit replayed onto a newer main (new commit ids)"),
    ("unmerged", unmerged, "keep", "a commit that is not in main"),
    ("merged-then-more", merged_then_more, "keep", "merged, then one more commit on the branch"),
    ("worktree-uncommitted", worktree_dirty, "keep", "merged branch, worktree has an uncommitted edit"),
    ("worktree-untracked", worktree_untracked, "keep", "merged branch, worktree has an untracked file"),
    ("worktree-ignored-env", worktree_ignored_env, "keep", "merged branch, worktree holds the only copy of an ignored .env"),
    ("protected-branch", protected_branch, "keep", "develop, a shared branch at the same commit as main"),
    ("remote-open-pr", remote_open_pr, "keep", "remote branch already in main but its pull request is still open"),
    ("local-main-only", local_main_only, "keep", "merged into local main, which was never pushed"),
]


def fake_gh(tmp: Path, heads: list[str]) -> str:
    script = tmp / "gh"
    script.write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps({[{'headRefName': h} for h in heads]!r}))\n")
    script.chmod(0o755)
    return str(script)


def tidy_verdict(r: Repo, kind: str, name: str, gh: str) -> dict:
    plan_file = r.path.parent / "plan.json"
    env = {**ENV, "GIT_TIDY_GH": gh}
    p = subprocess.run([sys.executable, str(SCRIPT), "plan", "--repo", str(r.path), "--no-fetch", "--min-idle-hours", "0",
                        "--plan-file", str(plan_file)], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                       timeout=300)
    if p.returncode:
        raise RuntimeError(f"git-tidy plan failed: {p.stderr}")
    items = json.loads(plan_file.read_text())["items"]
    item = next((i for i in items if i["kind"] == kind and i["name"] == name), None)
    if item is None:
        return {"verdict": "not listed", "deletes": False, "codes": []}
    return {"verdict": item["verdict"], "deletes": item["verdict"] == "SAFE", "codes": item["codes"]}


def one_liner_verdict(r: Repo, kind: str, name: str) -> dict:
    if kind == "remote":
        listed = git(r.path, "branch", "-r", "--merged").split()
    else:
        listed = [b.strip("*+ ") for b in git(r.path, "branch", "--merged").splitlines()]
    listed = [b for b in listed if b not in ("main", "master", "origin/main", "origin/master", "origin/HEAD", "->")]
    branch = name
    if kind == "worktree":
        branch = next(w.split("refs/heads/", 1)[1] for w in git(r.path, "worktree", "list", "--porcelain").split("\n\n")
                      if f"worktree {name}" in w)
    return {"deletes": branch in listed}


def main() -> int:
    rows = []
    for sid, build, truth, what in SCENARIOS:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d).resolve()
            r = Repo(tmp)
            kind, name = build(r)
            gh = fake_gh(tmp, ["feat"] if sid == "remote-open-pr" else [])
            tidy = tidy_verdict(r, kind, name, gh)
            usual = one_liner_verdict(r, kind, name)
        shown = name.replace(str(tmp), "<tmp>")  # no machine-specific paths in the result file
        rows.append({"id": sid, "what": what, "kind": kind, "item": shown, "truth": truth, "git_tidy": tidy, "one_liner": usual})

    def score(key):
        wrong = [x["id"] for x in rows if x[key]["deletes"] and x["truth"] == "keep"]
        missed = [x["id"] for x in rows if not x[key]["deletes"] and x["truth"] == "delete"]
        return {"deletes_what_should_stay": len(wrong), "keeps_what_could_go": len(missed),
                "wrong_ids": wrong, "missed_ids": missed}

    result = {
        "date": time.strftime("%Y-%m-%d"),
        "git_version": git(ROOT, "--version"),
        "python": sys.version.split()[0],
        "scenarios_total": len(rows),
        "should_delete": sum(1 for x in rows if x["truth"] == "delete"),
        "should_keep": sum(1 for x in rows if x["truth"] == "keep"),
        "summary": {"git_tidy": score("git_tidy"), "one_liner": score("one_liner")},
        "scenarios": rows,
    }
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{result['date']}-scenarios.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    for key in ("git_tidy", "one_liner"):
        s = result["summary"][key]
        print(f"{key}: deletes what should stay {s['deletes_what_should_stay']} {s['wrong_ids']}; "
              f"keeps what could go {s['keeps_what_could_go']} {s['missed_ids']}")
    print(f"wrote {out.relative_to(ROOT)}")
    return 1 if result["summary"]["git_tidy"]["deletes_what_should_stay"] else 0


if __name__ == "__main__":
    sys.exit(main())
