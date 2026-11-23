---
name: git-tidy
description: Use when a repository has piled up finished worktrees, local branches or remote branches to clean up. Judges by code whether each change is already in trunk (still recognised after rebase or squash merges change the commit ids), deletes only what cannot lose work, prints the list first, re-checks each item right before deleting it and writes a restore record.
---

# Clean up worktrees and branches safely

Script: `scripts/git_tidy.py` in this skill's folder. Run it with `python3 <skill folder>/scripts/git_tidy.py`; it needs only Python 3.9+ and git 2.38+ (`gh` is optional, for open pull requests). The script does all judging and deleting. Your job is to read the list, decide whether deletion is authorised, and report to the user.

TRIGGER: "clean up merged branches", "too many worktrees, delete what can go", "tidy the remote branches", "remove the finished worktrees".
DO NOT TRIGGER: "delete the feat/x branch" (one named branch: just do what the user said), "this worktree is broken, start over" (not a cleanup), "merge this PR" (not a cleanup).

## What counts as safe

An item is SAFE only when every row that applies holds; otherwise it is KEEP with the reason written out.

| Item | Condition |
|---|---|
| all | The code is in trunk: the commit is an ancestor of trunk; or merging it into trunk changes nothing; or every commit has an equivalent patch in trunk (rebase merge); or the whole branch as one patch has an equivalent commit in trunk (squash merge) |
| all | The name is not protected (main, master, develop, the release family and so on); the main checkout is never touched |
| worktree | No uncommitted changes and no untracked files; ignored files are only regenerable caches, or are registered as disposable or to be copied out first |
| worktree | Not locked; no process has its current directory inside; no git activity in the last 12 hours |
| local branch | No change in the last 12 hours; a branch checked out in a worktree is judged together with that worktree |
| remote branch | No open pull request (kept when PR state cannot be read); the latest commit is by you (others' branches only with `--include-others`) |

Trunk is the remote branch `origin/HEAD` points to, not your local main. A branch merged only into an unpushed local main is not safe.

## Steps

1. Run plan (read-only; it fetches first):

   ```
   python3 <skill folder>/scripts/git_tidy.py plan --repo <repo>
   ```

   Add `--local-only` when the user asked about local items only. The plan file goes to `<git-common-dir>/git-tidy/plan-latest.json` by default.

2. Report the list to the user: how many items can go and how many stay, with the kept ones grouped by reason (not merged, has changes, in use by a session, has an open PR...). Leave the long per-item list in the command output; do not repeat it in your reply.

3. Decide whether to delete:

   | Situation | What to do |
   |---|---|
   | The user asked for a cleanup in this turn | Apply all SAFE items, remote included, without asking again |
   | The user said only "look" or "list them" | Report the list; do not apply |
   | You decided a cleanup is due; the user did not ask | Report the list and ask whether to apply |
   | The user names a KEEP item to delete | Tell them its keep reason as written; if they insist, handle it as a separate manual step and do not change the script's verdict |

4. Apply:

   ```
   python3 <skill folder>/scripts/git_tidy.py apply --repo <repo>
   ```

   Apply fetches and judges again, and deletes only items that are still SAFE with an unchanged commit. Remote deletion uses a lease, so a branch that received a new push in the meantime is not deleted. Apply reuses the plan file's options (threshold, `--include-others`, `--local-only`). To change a threshold, run plan again with the flag, and do not run a plain plan afterwards, which would overwrite the plan file. Apply may take the same flags, only as a check: when they differ from the plan file it refuses and deletes nothing. The output ends with a `== Summary ==` section: what was deleted, why items were skipped or failed, what is left grouped by reason, and where the restore record is.

5. Check: run plan again and confirm the safe count dropped to zero or only new items appear.

6. Close with one summary after the last deletion of this cleanup. When the user approved further rounds midway, wait for the last round and merge all rounds into one summary. Take the `== Summary ==` sections of every apply and write, in this order:

   - First sentence: how many were deleted, skipped and failed; if any were skipped or failed, say which and why first.
   - What was deleted, grouped as worktrees, local branches, remote branches. Name any forced removal (submodules).
   - What is left, grouped by reason, with a count and names ("and N more" when long). For items kept only because of ignored files, say how to release them.
   - New `git-tidy.disposable` and `git-tidy.salvage` patterns registered this time, if any.
   - Where copied-out files went, and how many were not copied because they equal the main checkout.
   - Every restore record path, one per round.

## Worktrees kept only because of ignored files

Plan ends with the top-level paths behind these worktrees. They are usually tool state and logs, but can be things that still matter, such as `.env` or unprocessed data.

That the files matter does not mean the worktree must stay. Each path has three options; look at its content before asking the user:

| Option | Fits | Register |
|---|---|---|
| Drop | tool state, logs, lock files, build output | `git-tidy.disposable` |
| Copy out first | records still useful, unprocessed data, configuration such as `.env` | `git-tidy.salvage` |
| Keep the worktree | content you cannot judge, or that a person must handle first | nothing |

```
git -C <repo> config --add git-tidy.disposable '<pattern>'
git -C <repo> config --add git-tidy.salvage '<pattern>'
```

Before asking, check yourself: what the path holds, how big it is, and whether the main checkout has the same content. When the worktree's configuration is newer than the main checkout's, tell the user separately.

Copy-out: before removing a worktree, apply copies the matching files to `<git-common-dir>/git-tidy/salvage/<time>/<worktree name>/` and records the paths in the restore record's `salvaged` field. A path whose content equals the main checkout's is not copied. If any copy fails, that worktree is not removed and is reported as failed. A path matching both patterns is copied out.

Patterns are globs matched against the ignored file's relative path or top-level name. A directory may be written `.cache-x` or `.cache-x/`; a leading `./` and trailing `/` are removed. When a whole directory is ignored (say `notes/`), a pattern may point inside it (`notes/.local`, `notes/*`) and the script expands it to judge each entry. The glob `*` crosses `/`, so a same-named directory in subfolders is `*/.agent-runs`.

Paths the user has not confirmed stay kept. To release them for one run only, pass `--disposable <pattern>` or `--salvage <pattern>` to plan.

## Notes

- Worktrees are removed with git's own `worktree remove`, and branches are compared by commit before deletion, so git's own safeguards still apply. The one exception: git always refuses to remove a worktree with initialised submodules; the script first confirms the submodules have no changes, no untracked files and no non-cache ignored files, then forces.
- When your shell sits inside a worktree, that worktree is kept as "in use by a process". To clean it, run from the main checkout.
- Plan writes two things: fetch updates remote-tracking branches, and the squash test leaves a few unreferenced temporary commits in the object store (git gc removes them). It touches no working files.
- Restore: every item in the restore record has a ready restore command. git gc keeps deleted commit objects for about two weeks, so restoring works until then.
- When several sessions share one repository, a worktree another session just created and has not committed yet is protected by the 12-hour activity rule and the in-use rule. Lower `--min-idle-hours` only when the user wants a more aggressive cleanup.
