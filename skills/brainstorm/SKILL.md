---
name: brainstorm
description: Use when the user wants many genuinely different ideas for an open question (brainstorm, ideation, "what else could we try", "give me options I haven't thought of") and is willing to spend many sub-agent calls on it. Runs isolated parallel sub-agents through a baseline, two rounds and up to three pressure levels, groups the ideas by mechanism, and hands back every distinct mechanism, with those the plain baseline already produced folded at the end. It only diverges; it never scores, ranks or deletes ideas. Not for choosing between known options, fixing a bug, or a quick list of a few ideas.
---

# Brainstorm

A script holds the run state, the random assignments and every sub-agent prompt. Your job is to feed it, dispatch the sub-agents it asks for, and relay the result. Do not write, edit or judge ideas yourself.

The flow and its prompts are ported from the "full" configuration that a person scored. Differences from that configuration are listed under "Differences from the scored flow" below. Read "Evidence and limits" in `README.md` next to this file before you promise the user anything.

## What happens

1. **Baseline**: 3 batches of 3 sub-agents with no method (each still gets a random half of the cards, as in the scored run). Every later idea that lands on a baseline mechanism counts as "already obvious". This baseline belongs to the run; it is not the card-free "plain asking" control of the README.
2. **Round 1**: 3 sub-agents, each with a different method and a random half of the cards. Each lists titles, rewrites them to differ in mechanism, then expands them. These agents see no earlier ideas.
3. **Round 2**: the same, plus one idea per mechanism found so far to avoid, the decompositions the agents reported, and the titles of ideas no one else had.
4. **Pressure 1 to 3** (full profile only): avoid every mechanism seen so far; prefer cards no agent has received; attack the question's premises.

In every level after the baseline, ideas that repeat a baseline mechanism are returned once: the script issues a rewrite task for a fresh sub-agent with the same method and cards. Dispatch it like any other task; do not message the agent that wrote the original.

Pressure 2 seldom has an unused card to give. The nine baseline agents each receive half the cards, so with ten cards every card is normally taken before pressure 2, and its agents get reused cards. This is how the scored flow behaved.

Grouping is done by sub-agents too: once after each batch of new ideas (three times in the baseline), and once more after the rewrites of a level; a batch of more than 40 ideas is split. Expect about 12 grouping tasks in a full run. Grouping only merges ideas; every phrasing is kept and visible in the report.

## Profiles

- **full** (default): baseline through pressure 3. This is the flow that was counted against plain asking in the README.
- **economy**: stops after round 2. Not measured on its own.

Before pressure 1 the script stops and asks you to confirm. Tell the user that cost rises sharply from here, using the message the script prints, and continue only if they agree.

## Steps

The script is `scripts/brainstorm.py` in this skill's folder. It needs only Python 3 and its standard library. Below, `BS` means `python3 <this skill's folder>/scripts/brainstorm.py`.

1. **Question.** Write the user's question to a file exactly as they gave it. Do not rephrase, translate, summarise or fix it; agents see the original question on purpose. Collect hard constraints (things an idea must never violate) only if the user stated them.
2. **Cards (optional).** If the user supplied material, cut it into short self-contained cards, one fact or observation each, keeping their wording. Save them as a `.jsonl` file of `{"id": "c01", "text": "..."}` rows, or as a plain text file with one card per paragraph. A row has exactly the keys `id` and `text`; an id is 1 to 40 of the characters `A-Z a-z 0-9 _ . -`. Each agent gets half the cards, rounded down, and at least one. No material is fine: run without `--cards`. You may also gather material yourself, but say so to the user. Both the no-card case and host-gathered cards are untested: every measured run used 10 cards the author wrote in advance.
3. **Init.** Pick a new, empty run directory (for example `brainstorm-runs/<short-slug>` in the working directory; a full run writes about a hundred small files there, so inside a git repository tell the user, or use a directory git ignores) and run:
   `BS init --dir <run-dir> --question-file <file> [--constraint "..."]... [--cards <file>] [--methods library] [--profile economy] [--ideas-per-agent N]`
4. **Loop.** Run `BS next --dir <run-dir>` and act on the JSON it prints:
   - `"action": "dispatch"`: start one sub-agent per entry in `tasks`, **all in a single reply so they run in parallel**. Send each one only this message, with the path filled in: `Read the file <prompt> and do exactly what it says.` Wait until every one of them has finished, then run `next` once. Never run `next` twice in one command.
   - `"action": "wait"`: you ran `next` before every answer file existed. Nothing was changed. Wait for the sub-agents listed in `missing`, or start the ones that never ran, then run `next` again. Only when a task cannot be completed (its sub-agent failed twice, say), run `BS next --dir <run-dir> --give-up`: the missing outputs are dropped and recorded. A missing grouping answer is not dropped at once: its task is issued one more time first.
   - `"action": "confirm_pressure"`: show the user the `message`, ask whether to continue, then run `BS next --dir <run-dir> --pressure go` or `--pressure stop`.
   - `"action": "done"`: go to step 5. The same JSON is printed again if you run `next` after the end.
   - The command prints `error: ...` and exits with code 1: nothing was changed. Read the message; it names the cause (for example a pressure choice that was already made). Do not edit `state.json`.
5. **Report.** Give the user the path of `report.md` and a short summary from the `done` JSON: how many mechanisms (`mechanisms`), how many are beyond the baseline (`beyond_baseline`), and how many idea agents were dropped (`dropped_agents`; `failures` also counts partial rewrites and grouping problems, listed at the end of the report). "Beyond the baseline" compares with this run's own baseline step. The README's "skill-only" numbers compare with a separate plain-asking run, so the two are not the same count. Then show the "beyond the baseline" part of the report's contents list, and the full text of whichever mechanisms the user picks, or the whole file if they ask. Do not reorder, filter, score or merge anything.

If you lose track, `BS status --dir <run-dir>` prints progress without changing anything. Running `next` again resumes from the saved state.

## Sub-agent rules

- Each sub-agent must be fresh and isolated: no access to this conversation. Never use a fork-style sub-agent that inherits the parent conversation; seeing the user's chat or other agents' answers defeats the design.
- Use a sub-agent type that can read and write files. If a sub-agent replies with the JSON instead of writing it, save its reply to the task's `output` path unchanged. Never write or edit an answer file yourself in any other way.
- Do not paste extra context, hints or examples into the sub-agent message. Everything an agent should see is already in its prompt file.
- An agent that writes something malformed is dropped for that step and recorded; the run continues. An agent that writes nothing blocks the run until you restart it or pass `--give-up`. A grouping answer that cannot be read at all is retried once; after that, the affected ideas each keep their own mechanism and are marked as not grouped.

## Differences from the scored flow

The configuration a person scored called one model directly, without tools. This skill differs from it in these ways:

- **Host sub-agents.** Your sub-agents replace the direct model calls. Each one reads its prompt file and writes one answer file, so the prompt's rule against tools became "use tools only for those two files".
- **No pressure 4.** The scored configuration named a fourth pressure level (a method no agent had used, plus extreme extra constraints). It made no calls in the scored runs, because all four methods were already used by then: each scored repetition had the same 24 idea calls as this skill. The level is not in this skill.
- **One card is still given.** With a single card, each agent gets it. The scored harness rounded half of one card down to none. With two or more cards the assignments are the same as in the harness.
- **Everything else is kept**: the prompts' wording and order, the seeded assignment of methods and cards, the baseline of 3 batches, and one rewrite for baseline collisions.

## Settings that leave the measured flow

- `--ideas-per-agent` (default 10, the measured value): lowering it keeps sub-agent output short enough for hosts that cut long replies, but the run then differs from what was measured.
- Running some agents on other models, or letting agents search the web, is off by default and has not been measured; do it only if the user explicitly asks.
- The four method instructions (analogy, reverse a premise, stand in the affected person's place, use idle resources) are kept word for word from the scored run, which was in Chinese. Agents still answer in the language of the question.
- `--methods library` uses the larger set in `methods/library.jsonl` (the four above plus more, such as substitute, eliminate, contradiction, first principles). `--methods <file>` uses the user's own set. In both cases every agent of a batch gets a different method, and a method is reused only after every other method was used. One trial run showed no gain that the repository's comparison could detect (see `README.md` next to this file, Evidence and limits): use it only when the user asks for more thinking methods, and say so in your summary.

## Adding thinking methods

A method is one line in a `.jsonl` file: `{"id": "short-name", "text": "the instruction"}`, with the same limits on keys and ids as a card. To extend the bundled set, add lines to `methods/library.jsonl`; no code changes. Keep a method only if it forces the agent to start from a different place: it must name an operation to perform (replace a part, drop a step, resolve a named contradiction), not a way to judge ideas. Methods that only rate or sort ideas (SWOT, cost-benefit, decision matrices) do not belong here.

## Output

`report.md` in the run directory opens with a contents list, one line per mechanism, so the user can scan before reading. Below it, every mechanism appears once, with all phrasings that were merged into it. Each phrasing shows its source level (`baseline`, `round1`, `round1 rewrite`, `round2`, `pressure1` to `pressure3`), agent, method and the cards it used. Mechanisms beyond the baseline come first, in order of first appearance (by batch, then agent); those the baseline already produced are folded at the end. The script refuses to write a report whose mechanism or idea count does not match the run state.
