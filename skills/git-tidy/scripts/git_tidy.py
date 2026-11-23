#!/usr/bin/env python3
"""Safely clean up git worktrees, local branches and remote branches: delete only what is already in trunk.

Safe means every change on the branch can be found in trunk's current code, or as an equivalent patch in
trunk's history. Being an ancestor of trunk is not the only test: after a rebase or squash merge the
commit ids change but the code is still there, and that counts too.

  plan   Read-only. Fetches, judges each item SAFE or KEEP, prints the list and writes a plan file.
  apply  Reads the plan file, judges every SAFE item again and deletes it only if its commit is unchanged;
         writes a restore record.
"""
from __future__ import annotations

import argparse
import filecmp
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROTECTED = ("main", "master", "develop", "dev", "trunk", "gh-pages", "production", "prod", "staging",
             "release", "release/*", "release-*", "hotfix/*")
# worktree remove also deletes ignored files; only these regenerable caches and dependencies may go with it
REGENERABLE = {"node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target", "coverage",
               ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".next", ".nuxt", ".turbo",
               ".cache", ".parcel-cache", ".gradle", ".DS_Store", ".eslintcache", ".hypothesis"}
REGENERABLE_SUFFIX = (".pyc", ".pyo", ".tsbuildinfo", ".log")
GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}


@dataclass
class Item:
    kind: str            # worktree | prune | local | remote
    name: str            # worktree path / branch name / remote/branch name
    sha: str = ""
    branch: str = ""     # branch checked out in the worktree (empty when detached)
    verdict: str = "SAFE"
    how: str = ""        # why it is SAFE
    reasons: list = field(default_factory=list)
    codes: list = field(default_factory=list)    # machine-readable keep reasons, one per entry in reasons
    ignored: list = field(default_factory=list)  # ignored files that block deletion
    salvage: list = field(default_factory=list)  # ignored files to copy out before deletion
    submodules: bool = False  # has initialised submodules: git refuses to remove without --force; apply forces only after they are judged clean

    def keep(self, code, reason):
        self.verdict = "KEEP"
        self.codes.append(code)
        self.reasons.append(reason)


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=GIT_ENV,
                       stdin=subprocess.DEVNULL)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed (exit {r.returncode}): {r.stderr.strip()}")
    return r


def out(repo, *args):
    return git(repo, *args).stdout.strip()


def is_protected(name):
    return any(fnmatch.fnmatch(name, p) for p in PROTECTED)


# ---------- is the code already in trunk ----------

def containment(repo, base, tip):
    """Return (contained, reason or what is missing). Four tests from cheapest to dearest; stop at the first hit."""
    if git(repo, "merge-base", "--is-ancestor", tip, base, check=False).returncode == 0:
        return True, "commit is already in trunk"
    merged = git(repo, "merge-tree", "--write-tree", base, tip, check=False)
    if merged.returncode == 0 and merged.stdout.split("\n", 1)[0] == out(repo, "rev-parse", f"{base}^{{tree}}"):
        return True, "merging into trunk would change nothing"
    cherry = out(repo, "cherry", base, tip).splitlines()
    if cherry and all(line.startswith("-") for line in cherry):
        return True, "every commit has an equivalent patch in trunk (rebase merge)"
    mb = git(repo, "merge-base", base, tip, check=False).stdout.strip()
    if mb:
        squashed = out(repo, "commit-tree", f"{tip}^{{tree}}", "-p", mb, "-m", "git-tidy squash probe")
        if out(repo, "cherry", base, squashed).startswith("-"):
            return True, "the whole branch as one patch has an equivalent commit in trunk (squash merge)"
    ahead = len([c for c in cherry if c.startswith("+")])
    stat = out(repo, "diff", "--shortstat", mb, tip) if mb else "no common ancestor"
    return False, f"changes not in trunk: {ahead} commit(s), {stat or 'no file difference but the commits are not merged'}"


# ---------- live state ----------

def process_cwds():
    """Current directories (realpath) of all processes. None when unavailable; callers then keep as "cannot confirm"."""
    if not shutil.which("lsof"):
        if not Path("/proc").is_dir():
            return None
        cwds = set()
        for pid in Path("/proc").iterdir():
            if pid.name.isdigit():
                try:
                    cwds.add(os.path.realpath(os.readlink(pid / "cwd")))
                except OSError:
                    continue  # other users' cwd is unreadable; ceiling: sees only this user's processes, misses some on multi-user machines
        return cwds
    try:
        r = subprocess.run(["lsof", "-a", "-d", "cwd", "-F", "n"], capture_output=True,
                           encoding=sys.getfilesystemencoding(), errors="surrogateescape", timeout=30,
                           env={**os.environ, "LC_ALL": "C.UTF-8"})
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode:
        return None
    cwds = set()
    for line in r.stdout.split("\n"):
        if not line.startswith("n"):
            continue
        path = line[1:]
        # lsof escapes are ambiguous even in UTF-8 locales; never infer that a worktree is unused.
        # ceiling: literal backslash or caret blocks all worktree removals; relax only with a lossless cwd source.
        if "\\" in path or "^" in path:
            return None
        cwds.add(os.path.realpath(path))
    return cwds


def under(path, cwds):
    root = os.path.realpath(path)
    return any(c == root or c.startswith(root + os.sep) for c in cwds)


def hours_since(*paths):
    stamps = [p.stat().st_mtime for p in paths if p.exists()]
    return (time.time() - max(stamps)) / 3600 if stamps else float("inf")


def worktrees(repo):
    entries, cur = [], {}
    for line in out(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur:
                entries.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val or True
    return entries


def matches(rel_path, patterns):
    return any(fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(Path(rel_path).parts[0], pat) for pat in patterns)


def reaches_inside(rel_path, patterns):
    """A pattern points deeper inside rel_path (e.g. rel_path=cache, pattern cache/.local or cache/*)."""
    depth = len(Path(rel_path).parts)
    return any(len(Path(pat).parts) > depth and all(fnmatch.fnmatch(a, b) for a, b in
                                                    zip(Path(rel_path).parts, Path(pat).parts)) for pat in patterns)


def ignored_leftovers(path, disposable, salvage=None):
    """Ignored files that are not regenerable caches, returned in two lists: (blocking deletion, to copy out first).

    A match for git-tidy.salvage is copied out; a match for git-tidy.disposable is dropped; a match for both is copied out.
    """
    listed = git(path, "-c", "core.quotePath=false", "ls-files", "--others", "--ignored", "--exclude-standard",
                 "--directory").stdout.splitlines()
    # the parent's ls-files cannot see ignored files inside submodules; list each submodule and prefix its path
    listed += git(path, "submodule", "foreach", "--quiet", "--recursive",
                  'git ls-files --others --ignored --exclude-standard --directory | sed "s|^|$displaypath/|"',
                  check=False).stdout.splitlines()
    salvage, keep, move = list(salvage or []), [], []
    pending = list(listed)
    while pending:
        rel = pending.pop(0)
        rel_path = rel.rstrip("/")
        parts = Path(rel_path).parts
        if any(p in REGENERABLE for p in parts) or rel.endswith(REGENERABLE_SUFFIX):
            continue
        if matches(rel_path, salvage):
            move.append(rel_path)
        elif matches(rel_path, disposable):
            continue
        elif rel.endswith("/") and reaches_inside(rel_path, salvage + disposable):
            # ls-files folds a whole ignored directory into one line; when a pattern points inside, expand one level and judge each entry
            children = sorted(Path(path, rel_path).iterdir())
            pending[:0] = [f"{rel_path}/{c.name}" + ("/" if c.is_dir() and not c.is_symlink() else "")
                           for c in children]
        else:
            keep.append(rel)
    # when a directory holds only ignored subdirectories, ls-files lists both parent and children; copying the parent is enough, copying a child again hits File exists
    move = [m for m in move if not any(Path(m).is_relative_to(other) for other in move if other != m)]
    return keep, move


def disposable_patterns(raw):
    """Strip decorations such as `.health-cache/` or `./.env`: matching compares relative paths and top-level names, both without slashes."""
    return [q for q in (p.strip().removeprefix("./").rstrip("/") for p in raw) if q]


# ---------- plan ----------

def open_pr_heads(repo):
    """Set of head branch names of open PRs; (None, reason) when it cannot be read."""
    gh = os.environ.get("GIT_TIDY_GH") or shutil.which("gh")
    if not gh:
        return None, "gh is not installed"
    try:
        r = subprocess.run([gh, "pr", "list", "--state", "open", "--limit", "1000", "--json", "headRefName"],
                           cwd=repo, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"gh could not run: {e}"
    if r.returncode:
        return None, f"gh failed: {(r.stderr or r.stdout).strip()[:200]}"
    return {p["headRefName"] for p in json.loads(r.stdout or "[]")}, ""


def build_plan(args):
    repo = Path(out(args.repo, "rev-parse", "--show-toplevel"))
    common = Path(out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    remote = args.remote
    if not args.no_fetch:
        git(repo, "fetch", "--prune", "--quiet", remote)
    base = args.base or git(repo, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD", check=False).stdout.strip()
    if not base:
        base = next((f"{remote}/{b}" for b in ("main", "master")
                     if git(repo, "rev-parse", "--verify", "-q", f"{remote}/{b}", check=False).returncode == 0), "")
    if not base:
        raise SystemExit(f"no trunk found: {remote}/HEAD is not set and there is no {remote}/main or {remote}/master; pass --base")
    base_sha = out(repo, "rev-parse", base)
    me = out(repo, "config", "user.email") if git(repo, "config", "user.email", check=False).returncode == 0 else ""
    cwds = process_cwds()
    disposable = disposable_patterns(list(args.disposable or []) + git(
        repo, "config", "--get-all", "git-tidy.disposable", check=False).stdout.split())
    salvage = disposable_patterns(list(getattr(args, "salvage", None) or []) + git(
        repo, "config", "--get-all", "git-tidy.salvage", check=False).stdout.split())
    items, wt_branches = [], set()

    for i, wt in enumerate(worktrees(repo)):
        path = wt["worktree"]
        branch = wt.get("branch", "").removeprefix("refs/heads/") if isinstance(wt.get("branch"), str) else ""
        if branch:
            wt_branches.add(branch)
        if i == 0:
            continue  # never touch the main checkout
        if wt.get("prunable") or not Path(path).exists():
            items.append(Item("prune", path, branch=branch, how="directory is gone; only the record in git is cleared"))
            continue
        item = Item("worktree", path, sha=wt.get("HEAD", ""), branch=branch)
        items.append(item)
        if wt.get("locked"):
            item.keep("locked", f"locked: {wt['locked']}" if isinstance(wt["locked"], str) else "locked")
        if branch and is_protected(branch):
            item.keep("protected", f"checks out the protected branch {branch}")
        status = git(path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none", check=False)
        item.submodules = any(line and not line.startswith("-") for line in
                              git(path, "submodule", "status", "--recursive", check=False).stdout.splitlines())
        if status.returncode:
            item.keep("git_error", f"git status failed: {status.stderr.strip()[:120]}")
        elif status.stdout.strip():
            lines = status.stdout.splitlines()
            untracked = sum(1 for line in lines if line.startswith("??"))
            item.keep("dirty", f"{len(lines) - untracked} uncommitted change(s), {untracked} untracked file(s)")
        else:
            leftovers, item.salvage = ignored_leftovers(path, disposable, salvage)
            if leftovers:
                item.ignored = leftovers
                item.keep("ignored", "has ignored files that are not caches (removing the worktree deletes them): " + ", ".join(leftovers[:5])
                          + (" …" if len(leftovers) > 5 else ""))
        if cwds is None:
            item.keep("in_use", "cannot confirm whether a process uses it (lsof missing, failed, or its output had escapes)")
        elif under(path, cwds):
            item.keep("in_use", "a process has its current directory inside (a session may be using it)")
        gitdir = Path(out(path, "rev-parse", "--path-format=absolute", "--git-dir"))
        idle = hours_since(gitdir / "HEAD", gitdir / "index", gitdir / "logs" / "HEAD")
        if idle < args.min_idle_hours:
            item.keep("recent", f"git activity within {idle:.1f} hours (threshold {args.min_idle_hours} hours)")
        if item.sha:
            ok, how = containment(repo, base_sha, item.sha)
            item.how = how if ok else ""
            if not ok:
                item.keep("unmerged", how)

    for line in out(repo, "for-each-ref", "refs/heads", "--format=%(refname:short) %(objectname)").splitlines():
        name, sha = line.rsplit(" ", 1)
        if name in wt_branches or is_protected(name):
            continue
        item = Item("local", name, sha=sha)
        items.append(item)
        idle = hours_since(common / "logs" / "refs" / "heads" / name)
        if idle < args.min_idle_hours:
            item.keep("recent", f"changed within {idle:.1f} hours (threshold {args.min_idle_hours} hours)")
        ok, how = containment(repo, base_sha, sha)
        item.how = how if ok else ""
        if not ok:
            item.keep("unmerged", how)

    if not args.local_only:
        prs, pr_err = open_pr_heads(repo)
        base_branch = base.split("/", 1)[1]
        for line in out(repo, "for-each-ref", f"refs/remotes/{remote}",
                        "--format=%(refname:short) %(objectname) %(authoremail)").splitlines():
            name, sha, email = line.split(" ", 2)
            short = name.split("/", 1)[1] if "/" in name else name
            if short in ("HEAD", base_branch) or name == remote or is_protected(short):
                continue
            item = Item("remote", name, sha=sha)
            items.append(item)
            if prs is None:
                item.keep("pr_unknown", f"cannot read open PRs ({pr_err})")
            elif short in prs:
                item.keep("pr_open", "has an open PR; deleting the branch would close it")
            email = email.strip("<>")
            if not args.include_others and email != me:
                item.keep("other_author", f"latest commit is by {email}, not you ({me or 'user.email not set'}); add --include-others to delete")
            ok, how = containment(repo, base_sha, sha)
            item.how = how if ok else ""
            if not ok:
                item.keep("unmerged", how)

    return {"repo": str(repo), "common_dir": str(common), "remote": remote, "base": base, "base_sha": base_sha,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "options": {"min_idle_hours": args.min_idle_hours, "include_others": args.include_others,
                        "local_only": args.local_only, "base": base, "remote": remote,
                        "disposable": list(args.disposable or []),
                        "salvage": list(getattr(args, "salvage", None) or [])},
            "items": [asdict(i) for i in items]}


LABELS = {"worktree": "worktree", "prune": "stale record", "local": "local branch", "remote": "remote branch"}


def show(plan):
    items = plan["items"]
    safe = [i for i in items if i["verdict"] == "SAFE"]
    keep = [i for i in items if i["verdict"] == "KEEP"]
    print(f"repo {plan['repo']}  trunk {plan['base']} @ {plan['base_sha'][:8]}")
    print(f"options {options_line(plan['options'])}")
    print(f"{len(safe)} safe to delete, {len(keep)} kept\n")
    for title, group in (("== Safe to delete ==", safe), ("== Kept ==", keep)):
        print(title)
        for kind in ("worktree", "prune", "local", "remote"):
            for i in (g for g in group if g["kind"] == kind):
                extra = f" [{i['branch']}]" if i.get("branch") else ""
                why = i["how"] if i["verdict"] == "SAFE" else "; ".join(i["reasons"])
                if i["verdict"] == "SAFE" and i.get("salvage"):
                    why += "; copied out first: " + ", ".join(i["salvage"][:5]) + (" …" if len(i["salvage"]) > 5 else "")
                print(f"  {LABELS[kind]:<13} {i['name']}{extra}  {i['sha'][:8]}\n         {why}")
        print()
    only_ignored = [i for i in keep if set(i["codes"]) == {"ignored"}]
    if only_ignored:
        tops = {}
        for i in only_ignored:
            for rel in i["ignored"]:
                top = Path(rel.rstrip("/")).parts[0]
                tops[top] = tops.get(top, 0) + 1
        print(f"{len(only_ignored)} worktree(s) are already in trunk and kept only for ignored files. Paths (count):")
        print("  " + "  ".join(f"{k}({v})" for k, v in sorted(tops.items(), key=lambda kv: -kv[1])))
        print("  register what can go, so it no longer blocks: git config --add git-tidy.disposable '<pattern>'")
        print("  register what to keep as copied out first (to <git-common-dir>/git-tidy/salvage/): git config --add git-tidy.salvage '<pattern>'\n")


# ---------- apply ----------

# plan options apply accepts: only to check against the plan file, never to change the verdict
APPLY_CHECKED = (("min_idle_hours", "--min-idle-hours"), ("include_others", "--include-others"),
                 ("local_only", "--local-only"))


def same_content(a, b):
    """a and b have identical content (files byte by byte, directories recursively, symlinks by target)."""
    if a.is_symlink() or b.is_symlink():
        return a.is_symlink() and b.is_symlink() and os.readlink(a) == os.readlink(b)
    if a.is_file() and b.is_file():
        return filecmp.cmp(a, b, shallow=False)
    if a.is_dir() and b.is_dir():
        names = sorted(os.listdir(a))
        return names == sorted(os.listdir(b)) and all(same_content(a / n, b / n) for n in names)
    return False


def salvage_files(wt, sha, rels, main, dest_root):
    """Copy the ignored files to keep from a worktree into dest_root; return {relative path: destination}.

    A path whose content equals the main checkout's is not copied. Any failed copy raises OSError and the caller keeps the worktree.
    """
    dest_dir = dest_root / Path(wt).name
    if dest_dir.exists():  # same-named worktree under another parent
        dest_dir = dest_root / f"{Path(wt).name}-{sha[:8]}"
    result = {}
    for rel in rels:
        src, twin = Path(wt) / rel, Path(main) / rel
        if same_content(src, twin):
            result[rel] = f"same as {twin} in the main checkout; not copied"
            continue
        dest = dest_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, dest, symlinks=True)
        else:
            shutil.copy2(src, dest, follow_symlinks=False)
        result[rel] = str(dest)
    return result


def options_line(opts):
    return " ".join(f"{k}={opts.get(k)}" for k, _ in APPLY_CHECKED)


def apply(args):
    old = json.loads(Path(args.plan_file).read_text())
    opts = old["options"]
    asked = {k: getattr(args, k) for k, _ in APPLY_CHECKED if getattr(args, k) is not None}
    diff = {k: v for k, v in asked.items() if opts.get(k) != v}
    if diff:
        rerun = " ".join(f"{flag} {asked[k]:g}" if isinstance(asked[k], float) else flag
                         for k, flag in APPLY_CHECKED if asked.get(k))
        raise SystemExit(
            f"the plan file ({args.plan_file}) was made with {options_line(opts)}, "
            f"which differs from {', '.join(f'{k}={v}' for k, v in diff.items())} given now; "
            f"most likely a later plan run overwrote it. Nothing was deleted.\n"
            f"Run plan {rerun}".rstrip() + " first, read the list, then apply.")
    print(f"re-checking with the plan file's options: {options_line(opts)}")
    fresh_args = argparse.Namespace(repo=old["repo"], no_fetch=False, **opts)
    fresh = {(i["kind"], i["name"]): i for i in build_plan(fresh_args)["items"]}
    repo, remote = old["repo"], old["remote"]
    done, skipped, failed = [], [], []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    main_checkout = worktrees(repo)[0]["worktree"]
    salvage_root = Path(old["common_dir"]) / "git-tidy" / "salvage" / stamp
    salvaged = {}  # worktree path -> {relative path: destination}

    def recheck(i):
        now = fresh.get((i["kind"], i["name"]))
        if now is None:
            return "re-check: no longer exists"
        if now["verdict"] != "SAFE":
            return "re-check failed: " + "; ".join(now["reasons"])
        if now["sha"] != i["sha"]:
            return f"re-check failed: commit changed from {i['sha'][:8]} to {now['sha'][:8]}"
        return ""

    order = {"prune": 0, "worktree": 1, "local": 2}
    remotes = []
    for i in sorted((i for i in old["items"] if i["verdict"] == "SAFE"), key=lambda i: order.get(i["kind"], 9)):
        problem = recheck(i)
        if problem:
            skipped.append((i, problem))
            continue
        if i["kind"] == "remote":
            remotes.append(i)
            continue
        rels = fresh[(i["kind"], i["name"])].get("salvage") or []
        if rels:  # use the list from the re-check, so files that appeared after plan are included
            try:
                salvage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                salvaged[i["name"]] = salvage_files(i["name"], i["sha"], rels, main_checkout, salvage_root)
            except OSError as e:
                failed.append((i, f"copy-out failed, not deleted: {e}"))
                continue
        steps = {
            "prune": [["worktree", "prune"]],
            "worktree": [["worktree", "remove", *(["--force"] if i.get("submodules") else []), i["name"]]] + ([["branch", "-D", i["branch"]]] if i["branch"] else []),
            "local": [["branch", "-D", i["name"]]],
        }[i["kind"]]
        for n, step in enumerate(steps):
            print("+ git " + " ".join(step))
            r = git(repo, *step, check=False)
            if r.returncode:
                failed.append((i, f"git {' '.join(step)}: {r.stderr.strip()[:300]}"))
                if n:  # an earlier step took effect (worktree removed, branch not), so it must still be restorable
                    done.append(i)
                break
        else:
            done.append(i)

    # one push deletes many remote branches; the lease deletes each only if it still points at the planned commit, and each result stands alone
    for chunk in (remotes[k:k + 100] for k in range(0, len(remotes), 100)):
        refs = {f"refs/heads/{i['name'].split('/', 1)[1]}": i for i in chunk}
        cmd = ["push", "--porcelain", *[f"--force-with-lease={ref}:{i['sha']}" for ref, i in refs.items()],
               remote, *[f":{ref}" for ref in refs]]
        print(f"+ git push --porcelain --force-with-lease=… {remote} (deleting {len(refs)} remote branch(es))")
        r = git(repo, *cmd, check=False)
        status = {}
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[1].startswith(":refs/heads/"):
                status[parts[1][1:]] = (parts[0].strip(), parts[2])
        for ref, i in refs.items():
            flag, summary = status.get(ref, ("?", r.stderr.strip()[:300] or "push did not report this ref"))
            if flag == "-":
                done.append(i)
            else:
                failed.append((i, f"remote deletion not done: {summary}"))

    restore_dir = Path(old["common_dir"]) / "git-tidy"
    restore_dir.mkdir(exist_ok=True)
    restore_file = restore_dir / f"restore-{stamp}.json"
    restore = []
    for i in done:
        if i["kind"] == "worktree":
            cmd = f"git worktree add {i['name']} {i['branch']}" if i["branch"] else f"git worktree add --detach {i['name']} {i['sha']}"
            restore.append({**i, "restore": ([f"git branch {i['branch']} {i['sha']}"] if i["branch"] else []) + [cmd],
                            **({"salvaged": salvaged[i["name"]]} if i["name"] in salvaged else {})})
        elif i["kind"] == "local":
            restore.append({**i, "restore": [f"git branch {i['name']} {i['sha']}"]})
        elif i["kind"] == "remote":
            restore.append({**i, "restore": [f"git push {remote} {i['sha']}:refs/heads/{i['name'].split('/', 1)[1]}"]})
    restore_file.write_text(json.dumps({"repo": repo, "plan": str(args.plan_file), "items": restore},
                                       ensure_ascii=False, indent=2))

    summarize(done, skipped, failed, fresh, old, restore_file,
              {k: v for k, v in salvaged.items() if k in {i["name"] for i in done}}, salvage_root)
    return 1 if failed else 0


# an item may have several keep reasons; the summary files it under the first group that matches, in this order
KEEP_GROUPS = [("unmerged", "has commits not in trunk"), ("dirty", "has uncommitted changes"), ("pr_open", "has an open PR"),
               ("in_use", "in use by a session"), ("recent", "recently active"), ("ignored", "has ignored files"),
               ("locked", "locked"), ("protected", "protected"), ("other_author", "someone else's branch"),
               ("pr_unknown", "PR state unknown"), ("git_error", "git error")]


def names(items, limit=8):
    shown = [i["name"] + (f" [{i['branch']}]" if i.get("branch") else "") for i in items[:limit]]
    return ", ".join(shown) + (f" and {len(items) - limit} more" if len(items) > limit else "")


def summarize(done, skipped, failed, fresh, old, restore_file, salvaged, salvage_root):
    """Summary at the end of apply: what was deleted, what is left and why. What is left comes from the re-check at the start of apply."""
    print(f"\n== Summary ==\ndeleted {len(done)}, skipped after re-check {len(skipped)}, failed {len(failed)}")
    for kind in ("worktree", "local", "remote", "prune"):
        group = [i for i in done if i["kind"] == kind]
        if group:
            print(f"  deleted {LABELS[kind]} ({len(group)}): {names(group)}")
    if salvaged:
        copied = sum(1 for m in salvaged.values() for dest in m.values() if dest.startswith(str(salvage_root)))
        same = sum(len(m) for m in salvaged.values()) - copied
        print(f"  copied out first: {len(salvaged)} worktree(s), {copied} item(s) to {salvage_root}"
              + (f", {same} same as the main checkout and not copied" if same else ""))
    for i, why in skipped:
        print(f"  skipped {LABELS[i['kind']]} {i['name']}: {why}")
    for i, why in failed:
        print(f"  failed {LABELS[i['kind']]} {i['name']}: {why}")

    failed_keys = {(i["kind"], i["name"]) for i, _ in failed}
    keep = [i for k, i in fresh.items() if i["verdict"] == "KEEP" and k not in failed_keys]
    planned = {(i["kind"], i["name"]) for i in old["items"]}
    new_safe = [i for k, i in fresh.items() if i["verdict"] == "SAFE" and k not in planned]
    print(f"still left: {len(keep) + len(failed_keys)}" + (", by reason:" if keep or failed else ""))
    if failed:
        print(f"  deletion failed {len(failed)}: {names([i for i, _ in failed])}")
    for code, label in KEEP_GROUPS:
        group = [i for i in keep if next((c for c, _ in KEEP_GROUPS if c in i["codes"]), None) == code]
        if group:
            print(f"  {label} {len(group)}: {names(group)}")
    if new_safe:
        print(f"{len(new_safe)} item(s) became safe after plan and were not deleted; run plan again to see them")
    print(f"restore record: {restore_file} (git gc keeps the commit objects for about two weeks)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--repo", default=".")
    p.add_argument("--remote", default="origin")
    p.add_argument("--base", help="trunk; default <remote>/HEAD")
    p.add_argument("--min-idle-hours", type=float, default=12, help="keep anything with git activity within this many hours")
    p.add_argument("--include-others", action="store_true", help="also delete remote branches whose latest commit is not yours")
    p.add_argument("--local-only", action="store_true", help="ignore remote branches")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--disposable", action="append", metavar="GLOB",
                   help="extra ignored-file pattern to treat as disposable this run; for good, set git config git-tidy.disposable")
    p.add_argument("--salvage", action="append", metavar="GLOB",
                   help="extra ignored-file pattern to copy out before deletion this run; for good, set git config git-tidy.salvage")
    p.add_argument("--plan-file", help="default <git-common-dir>/git-tidy/plan-latest.json")
    a = sub.add_parser("apply")
    a.add_argument("--repo", default=".")
    a.add_argument("--plan-file")
    # apply reuses the plan file's options; these flags only check against it, and a mismatch refuses to run
    a.add_argument("--min-idle-hours", type=float, help="check only: must match the plan run")
    a.add_argument("--include-others", action="store_const", const=True, help="check only: must match the plan run")
    a.add_argument("--local-only", action="store_const", const=True, help="check only: must match the plan run")
    args = ap.parse_args()

    if not args.plan_file:
        common = Path(out(args.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        args.plan_file = str(common / "git-tidy" / "plan-latest.json")
    if args.cmd == "plan":
        plan = build_plan(args)
        Path(args.plan_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.plan_file).write_text(json.dumps(plan, ensure_ascii=False, indent=2))
        show(plan)
        print(f"plan file: {args.plan_file}")
        return 0
    return apply(args)


if __name__ == "__main__":
    sys.exit(main())
