---
name: brainstorm
description: Use when the user wants many genuinely different ideas for an open question (brainstorm, ideation, "what else could we try", "give me options I haven't thought of") and is willing to spend many sub-agent calls on it. Runs isolated parallel sub-agents through a baseline, two rounds and up to three pressure levels, groups the ideas by mechanism, and hands back every distinct mechanism, with those the plain baseline already produced folded at the end. It only diverges; it never scores, ranks or deletes ideas. Not for choosing between known options, fixing a bug, or a quick list of a few ideas.
---

# Brainstorm

A script holds the run state, the random assignments and every sub-agent prompt. Your job is to feed it, dispatch the sub-agents it asks for, and relay the result. Do not write, edit or judge ideas yourself.

The flow and its prompts are ported from the evaluated "full" configuration. Read "Evidence and limits" in the repository README before you promise the user anything.

## What happens

1. **Baseline**: 3 batches of 3 sub-agents with no method (each still gets a random half of the cards, as in the evaluated run). Every later idea that lands on a baseline mechanism counts as "already obvious".
2. **Round 1**: 3 sub-agents, each with a different method and a random half of the cards. Each lists titles, rewrites them to differ in mechanism, then expands them. Ideas that repeat a baseline mechanism go back to their agent once for a rewrite.
3. **Round 2**: the same, plus the decompositions the agents reported and the ideas no one else had.
4. **Pressure 1 to 3** (full profile only): avoid every mechanism seen so far; switch to cards no agent has used yet; attack the question's premises. Each level also returns baseline collisions once.

After every step, one sub-agent groups the new ideas by mechanism. Grouping only merges ideas; every phrasing is kept and visible in the report.

## Profiles

- **full** (default): baseline through pressure 3. This is the evaluated flow.
- **economy**: stops after round 2. Not evaluated on its own.

Before pressure 1 the script stops and asks you to confirm. Tell the user that cost rises sharply from here, using the message the script prints, and continue only if they agree.

## Steps

The script is `scripts/brainstorm.py` in this skill's folder. It needs only Python 3 and its standard library. Below, `BS` means `python3 <this skill's folder>/scripts/brainstorm.py`.

1. **Question.** Write the user's question to a file exactly as they gave it. Do not rephrase, translate, summarise or fix it; agents see the original question on purpose. Collect hard constraints (things an idea must never violate) only if the user stated them.
2. **Cards (optional).** If the user supplied material, cut it into short self-contained cards, one fact or observation each, keeping their wording. Save them as a `.jsonl` file of `{"id": "c01", "text": "..."}` rows, or as a plain text file with one card per paragraph. No material is fine: run without `--cards`. You may also gather material yourself, but say so to the user. Both the no-card case and host-gathered cards are untested: the evaluation used 10 cards the author wrote in advance.
3. **Init.** Pick a new, empty run directory (for example `brainstorm-runs/<short-slug>` in the working directory) and run:
   `BS init --dir <run-dir> --question-file <file> [--constraint "..."]... [--cards <file>] [--profile economy] [--ideas-per-agent N]`
4. **Loop.** Run `BS next --dir <run-dir>` and act on the JSON it prints:
   - `"action": "dispatch"`: start one sub-agent per entry in `tasks`, **all in a single reply so they run in parallel**. Send each one only this message, with the path filled in: `Read the file <prompt> and do exactly what it says.` Wait until every one of them has finished, then run `next` once. Never run `next` twice in one command.
   - `"action": "wait"`: you ran `next` before every answer file existed. Nothing was changed. Wait for the sub-agents listed in `missing`, or start the ones that never ran, then run `next` again. Only when a task cannot be completed (its sub-agent failed twice, say), run `BS next --dir <run-dir> --give-up`: the missing outputs are dropped and recorded.
   - `"action": "confirm_pressure"`: show the user the `message`, ask whether to continue, then run `BS next --dir <run-dir> --pressure go` or `--pressure stop`.
   - `"action": "done"`: go to step 5.
5. **Report.** Give the user the path of `report.md` and a short summary: how many mechanisms, how many are beyond the baseline, and any dropped agents. Then show the "beyond the baseline" part of the report's contents list, and the full text of whichever mechanisms the user picks, or the whole file if they ask. Do not reorder, filter, score or merge anything.

If you lose track, `BS status --dir <run-dir>` prints progress without changing anything. Running `next` again resumes from the saved state.

## Sub-agent rules

- Each sub-agent must be fresh and isolated: no access to this conversation. Never use a fork-style sub-agent that inherits the parent conversation; seeing the user's chat or other agents' answers defeats the design.
- Use a sub-agent type that can read and write files. If a sub-agent replies with the JSON instead of writing it, save its reply to the task's `output` path unchanged. Never write or edit an answer file yourself in any other way.
- Do not paste extra context, hints or examples into the sub-agent message. Everything an agent should see is already in its prompt file.
- An agent that writes something malformed is dropped for that step and recorded; the run continues. An agent that writes nothing blocks the run until you restart it or pass `--give-up`. A grouping answer that cannot be read at all is retried once; after that, the affected ideas each keep their own mechanism and are marked as not grouped.

## Settings that leave the evaluated flow

- `--ideas-per-agent` (default 10, the evaluated value): lowering it keeps sub-agent output short enough for hosts that cut long replies, but the run then differs from what was evaluated.
- Running some agents on other models, or letting agents search the web, is off by default and has not been evaluated; do it only if the user explicitly asks.
- The four method instructions are kept word for word from the evaluated run, which was in Chinese. Agents still answer in the language of the question.

## Output

`report.md` in the run directory opens with a contents list, one line per mechanism, so the user can scan before reading. Below it, every mechanism appears once, with all phrasings that were merged into it. Each phrasing shows its source level (`baseline`, `round1`, `round1 rewrite`, `round2`, `pressure1` to `pressure3`), agent, method and the cards it used. Mechanisms beyond the baseline come first, in order of first appearance; those the baseline already produced are folded at the end. The script refuses to write a report whose mechanism or idea count does not match the run state.
