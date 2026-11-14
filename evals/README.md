# Evaluation tools for the brainstorm skill

These tools measure the skill. They are not a skill and nothing here is installed with one.
Python 3.13 and the standard library only. File locking requires POSIX; Windows is not supported.

## What is here

There are two sets of tools. They were built at different times and answer different questions.

| | Counting against plain asking | The scoring pipeline |
|:---|:---|:---|
| Question it answers | With equal idea calls, how many mechanisms does the shipped skill reach that plain repeated asking does not? | Does a person find the ideas of one configuration better than those of another? |
| What it runs | The shipped skill, with a coding agent dispatching sub-agents | Configurations of the flow through direct model calls, paid through a spend ledger |
| Who judges | A grouping model, then a judge model | A person, blind, on a scoring page |
| Code | `brainstorm/skill_budget.py` | Every other module in `brainstorm/`, and `brainstorm/web/` |
| Used for | Every comparison with plain asking in the skill's README | The one human score in the skill's README, with its idea counts and costs |
| Use it today? | Yes. This is how the skill is measured now | Only to score with a person again. It was used once and then set aside because scoring cost the author too much effort |

Also here:

- `brainstorm/results/`: a summary file for every measurement, with a [README](brainstorm/results/README.md) that says which are current.
- `brainstorm/exams/v1/`: three questions with their hard constraints and ten cards each (`cli_onboarding`, `side_income`, `newcomer_questions`; the last was written and never run), and the configurations of the scoring pipeline.
- `brainstorm/BILLING.md`: the full contract of the spend ledger and the model backend.
- `brainstorm/tests/`: the tests of both sets.

What the results mean is in [skills/brainstorm/README.md](../skills/brainstorm/README.md#evidence-and-limits). This page is about how to run the tools.

## Counting against plain asking

`skill_budget.py` calls no model. Like the skill's own script, it prints tasks; the coding agent that runs it dispatches one sub-agent per task and runs the next command.

### The steps

1. **Run the skill** on the question, as a user would. Keep its run directory.
2. **Run plain asking**: the same question and the same hard constraints, with no cards, no methods and no rounds. Give it as many idea calls as the skill run made: `--batches` is that number divided by 3, rounded (a skill run of 31 calls gets 10 batches, 30 calls). `merge` refuses a control run whose constraints differ from the skill run's.
3. **Copy the finished plain-asking run**, so that there is one directory for each grouping you will make. Two groupings need the original and one copy.
4. **Merge** the skill run into each copy. Merging sends every idea of the skill run through the grouper, into the plain-asking mechanisms or into new ones. Use the strongest model you have for these grouping tasks.
5. **Compare** each copy, and **repeat** across the copies to see how far the grouping alone moves the count.
6. **Audit** each copy: a judge of a different model than the grouper checks every skill-only mechanism for repeats, twice.

```sh
SB="python3 -m evals.brainstorm.skill_budget"

# 2. plain asking
$SB direct-init --dir <control dir> --question-file q.txt --batches 10 \
    --constraint "<first hard constraint>" --constraint "<second hard constraint>"
$SB next --dir <control dir>                 # dispatch the printed tasks; repeat until "done"

# 3. one directory for each grouping: the original and one copy
cp -R <control dir> <second control dir>

# 4. merge the skill run into each copy
$SB merge --dir <control dir> --from <skill run dir>
$SB next --dir <control dir>                 # dispatch; repeat until "done"
#    ... the same two commands for <second control dir>

# 5. counts
$SB compare --dir <control dir>
$SB repeat --dir <control dir> --dir <second control dir>

# 6. audit, for each copy
$SB audit-init --dir <control dir> --sample 500
#    give the printed prompt file to one judge agent; it writes the answer file
$SB audit-read --dir <control dir>
#    keep the answer file under another name, run the judge again on the same prompt, read again
```

### What each command reports

- **`compare`**: the mechanisms only the skill reached (`skill_only`), only plain asking reached, and both; and how fast plain asking stops finding new mechanisms. `ungrouped_mechanisms` counts, by side, the mechanisms whose first idea no grouper placed because a grouping answer was unusable. Such an idea was never compared with the mechanisms before it, so on the skill-only side it may be a repeat that inflates the count. Group again when there are more than a few.
- **`repeat`**: the skill-only count of each grouping and its range; and how many ideas of the skill run got the same verdict in two groupings (plain asking reached it, or did not). It refuses directories that do not hold the same ideas.
- **`audit-init`**: writes one prompt with two parts. Part 1 lists every mechanism plain asking reached and the skill-only mechanisms to check; the judge names repeats under one fixed rule (`SAME_RULE` in the script, the same for every question). Part 2 lists all mechanisms in random order with no source label; the judge names the hard constraints each one breaks. The source is not truly hidden: an item of part 2 that also stands in part 1 can be matched there, and when every skill-only mechanism is checked that is every item. `--sample` larger than the skill-only count checks every skill-only mechanism. If an answer file is already there and the new prompt differs from the old one, the old answer is set aside under another name so it cannot be read against the new prompt.
- **`audit-read`**: reports the repeats, the skill-only count left after them (`estimated_new`), and constraint violations by side. When the answer is incomplete or malformed it exits with code 1: it prints `"ok": false` with a list of problems, or `error: ...` when the file is not one JSON object. Do not use the numbers of such a result. When a label is answered twice, the last answer counts.
- **`check`**: a smoke check of one skill run, for use before a release: `$SB check --dir <skill run dir>`. It fails if the run is unfinished, an idea agent was dropped, a grouping step left ideas ungrouped, a level is missing, or a level added fewer than `--min-new` mechanisms beyond the baseline.

### Rules for a count you can report

- **Group with the strongest model, twice.** Report the range, not one count. Measured once on 258 ideas: three Sonnet groupings gave 58, 76 and 91 skill-only mechanisms (84% to 91% of ideas with the same verdict); two Opus groupings gave 74 and 76 (96%).
- **Audit every skill-only mechanism, twice.** A sample of 25 gave 5 and 16 repeats on two groupings that agree on 96% of ideas.
- **The judge is the least steady part.** Two passes of one judge over the same 99 mechanisms called 17 and then 60 of them repeats. A difference between two runs counts only when it is larger than the difference between two judge passes over one run.
- **Write a results file** under `brainstorm/results/` and add it to the README there. A number in a skill README must be in one of those files.

### Limits of this method

- Calls are equal, tokens are not: the skill's later prompts are far longer.
- The plain-asking ideas are grouped first, so a borderline skill idea is more likely to be merged into a plain-asking mechanism than the other way round.
- Mechanism counts depend on the grouping model and on the judge.
- The constraint check is not blind (see `audit-init` above).
- None of this measures usefulness.

## The scoring pipeline

Everything below this heading is about the second set of tools. It records ideas through direct model calls, merges them by mechanism, computes machine metrics, issues blind scoring sheets, and decides whether a change to the flow helped.

### Quick Start

```sh
python3 -m evals.brainstorm e2e --fake --run-dir /tmp/brainstorm_e2e
```

This chains the stages on the fake backend, without network or money: recording,
mechanism grouping, calibration against simulated human groups, constraint checks,
one replay per exam, metrics, sheets, simulated answers, reveal and the report.
Simulated answers call the same functions as the scoring page but bypass the page
itself, whose server has its own tests. It writes
`report.html` (open this), `report.md` with the same content, `report.json` and
`e2e_summary.json` into the run directory.
Its decision **only proves the wiring**. It is not a finding about any method.
`--fake` is mandatory because this command has no real-backend mode on purpose.

With real recordings, build the report yourself:

```sh
python3 -m evals.brainstorm report --run-dir RUN --exam-dir EXAMS \
  --config old.json --config new.json --baseline old --candidate new \
  --decision-exams exam_a,exam_b,exam_c
```

- `--decision-exams` has no default. Exams do not say which are development exams,
  and defaulting to "the exams with answers" could never detect a missing answer.
- `report` is read-only for money. Only `--judge-config` pays, for hard-constraint
  checks through the ledger, and a repeat run reuses the paid result.
- The main score needs exactly one revealed comparison sheet whose units cover exactly
  the two compared configs. None or several count as missing answers.
- Hard-constraint violations are judged exam by exam: a rise on any decision exam rejects
  the candidate, and the report names that exam. Cost is compared as a total.
- Thresholds are provisional, and can be changed: `--cost-ratio` (1.5),
  `--check-bad-ratio` (0.34), `--contribution-floor` (0).

### Reading The Report

The report has three layers, for someone deciding which stage to change:

1. **Start here**: the conclusion, the next step and how far to trust it, in five lines at most.
2. **Stage table**: one row per stage, with how many scored ideas needed it, what a removal
   comparison showed and one of four pieces of advice: keep, try removing, can delete, no data.
   A removal nobody has run says "not tested", never zero.
3. **Detail**: every other section, folded.

"Try removing" is correlation only. To test one, copy `drop_config` from that stage's row in
`leave_one` in `report.json` into a file and keep its `<config>__drop_<stage>` name. Record it
with `loop`, issue and answer a comparison sheet over the original and that config, then pass
it as one more `--config` to `report`. The stage's row then shows the result. A removal with no
worse result is "can delete".

### Sheets And Pages

```sh
python3 -m evals.brainstorm sheet --kind group --run-dir RUN --exam-dir EXAMS \
  --exam-id exam_a --sheet-id group_1 --seed 1
python3 -m evals.brainstorm serve --run-dir RUN --sheet-id group_1
```

- Every sheet needs `--exam-dir`, grouping sheets too: both pages show the exam's question
  and hard constraints at the top, exactly as written. Judging "had you thought of this" is
  impossible without the question.
- Nothing else from the exam reaches the page. Hidden answers, the wrong premise, planted
  probes and the exam id stay out; so do lineage and internal IDs.
- The question is not masked. It is the same above every item, so it cannot tell one item's
  source from another's. If it names a config, model, method or card, `sheet` warns.
- A sheet issued before this change has no question. It still opens and can be revealed, and
  both `serve` and the page say the question is missing. To see it, issue a new sheet. A
  score sheet must be revealed first, because an exam allows one open score sheet.

### Writing Exams

- `check_samples` must each be a **title line**, a newline, then a body. Real items look
  like that, and a one-line sample would stand out, so it is left out with a warning.
- Use descriptive names for configs, methods and models. Text that repeats such a name is
  masked on full and group sheets, and refused on comparison sheets.
- Imported units do not count as complete. Importing records their cost and ideas, but
  provides no evidence of a finished population for metrics, comparison or replay.

### How Money Is Controlled

- **The hard limit is checked before each call, against the worst case.** A call
  reserves `(fixed overhead + input estimate + max output) x price`. If that would
  cross the limit, the call never starts and the command stops with exit code 3.
- **A single call can still exceed its reservation**, because providers do not enforce
  it. The real cost is recorded, every later call on that ledger stops, and the
  command reports the overrun. Raising the limit does not clear this.
- Stopping at the limit loses nothing. Rerun the same command with a higher limit
  (`--budget-usd` for `e2e`, `--limit` elsewhere): finished calls replay from cache
  without paying again.
- A process killed during a call is different. The call stays uncertain and holds its
  reservation until you reconcile it by hand, and the two commands lead to different places:
  - `ledger release` says no charge happened. It needs independent evidence, and the run can
    then resume. Do not release a call just to continue: a request that was already sent has
    usually been charged.
  - `ledger settle` records the bill, nothing more. That call cannot be retried. In recording,
    grouping inside a loop or a constraint check its unit becomes a failed unit, as below.
    Calibration, replay and rewriting have no unit to replace: start calibration or replay
    under a new name, or issue the sheet again with a higher `--rewrite-attempt`. Each pays
    again and the first charge stays. Grouping inside a loop also keeps the older choice of
    `dedupe run --retry-paid-error`, at most two attempts in total.
- All real backends share one cumulative ledger, whatever the run directory.
  Do not start a new ledger to get around it.

### Replacing A Failed Unit

A failed unit blocks its config: a comparison sheet covers every repetition of a config, so
one failed unit refuses the sheet and the decision stays `indeterminate`. Another repetition
alone does not help. You can replace the failed one, only once, and only by asking:

```sh
python3 -m evals.brainstorm replace-unit --run-dir RUN --exam-dir EXAMS --exam-id exam_a \
  --config new.json --dedupe-config classifier.json --failed-rep 2 --new-rep 4
```

- Nothing retries by itself. No other command starts a replacement.
- Only a unit that truly failed qualifies: a paid failure, output unusable on both attempts,
  a grouping result that could not be used, or a failed constraint check. A unit stopped by
  the limit can resume, so it is refused. An uncertain call must be reconciled first.
- A failed unit is replaced only once. If the replacement fails too, the command stops and
  reports, the record stays, and neither unit can be replaced again. The way on is
  `abandon-unit` (see `python3 -m evals.brainstorm abandon-unit --help`).
- The failed unit's files and cost stay, and the ledger is only appended to. The seed comes
  from the failed unit, and the config, classifier and ledger must match it, so none of them
  can be changed to erase a failure. The new repetition reserves budget like any other, and
  rerunning the same command resumes it after a limit stop without paying twice.
- `<run-dir>/<exam_id>/replacements.jsonl` holds the record. Sheets, the guard counts and the
  main score use the replaced set, so counts stay equal. A sheet issued before the replacement
  no longer counts: issue a new one, and answers already given are reused.
- Grouped ideas of the failed unit stay in the mechanism table and the full view. Its ideas that
  were never grouped stay on disk and out of every sheet, and the report counts them.
- The report says which unit was replaced and what both units cost, at the top and in the
  detail. The failed unit's cost is in the totals, not in the decision's cost comparison.
- A standalone `record` unit has no loop to rerun, so it cannot be replaced.

### Where Data Lives

- The real ledger is outside every worktree, at
  `$XDG_STATE_HOME/xiaolang-skills/brainstorm/ledger.real.jsonl`, or
  `~/.local/state/xiaolang-skills/brainstorm/ledger.real.jsonl` when that is unset. Its lock and
  cache sit next to it. It is the only record of what was spent, so never delete it.
- A ledger left at the old path, `evals/brainstorm/runs/ledger.real.jsonl`, stops every real
  backend until it is moved, because a new empty ledger would reset the limit. The error
  prints the move command. Nothing moves it for you. `--ledger` still picks any file you name.
  A run recorded on the old path keeps that path in its state, so finish it with `--ledger`
  or start a new run.
- `evals/brainstorm/runs/` holds run directories and
  `evals/brainstorm/exams/private/` holds private questions. Git ignores both. Never
  run `git clean` on them.
- A fake run keeps its own `ledger.fake.jsonl` inside the run directory.
- `<run-dir>/sheets/<id>/sheet.issued.json` fixes every ID a sheet must hold when it is
  issued. Reveal and the report compare against it as sets, so a lost row fails even if the
  counts were lowered to match. The scoring page never reads it.
- `<run-dir>/<exam_id>/mechanisms.json` is the only authority for mechanism IDs.
  `replay/` holds replay and calibration outputs, which are never reused for verdicts.

### What Ran For Real, And What Is Not Verified

Ran on a real backend, once, for the human score in `brainstorm/results/2026-11-08-human-scored-cli.json`:
recording of the `direct` and `full` configurations (three repetitions each on one question), mechanism
grouping, one comparison sheet with rewritten items, the scoring page, reveal, and the spend ledger. The
model was Sonnet through the Claude backend.

Not verified:

- Backends other than Claude, such as a generic command backend pointing at another tool.
  Those paths have only been tested against local fake processes.
- Calibration, constraint checks, replay, the removal comparison and unit replacement on a real backend.
  They have only run on the fake one.
- The `report` decision on real data. The human score was read from the revealed sheet; no report was
  built from it.
- Rewrite quality and grouping quality on real text. Both ran, and nobody measured how good they were.
  The steadiness of grouping was measured later, with the other set of tools (see above).
- Running a real backend from inside an agent session. The adapter passes its whole
  environment to the subprocess, including that session's variables.
- Bigram TF-IDF is a rough lexical estimate, not semantic coverage.
- Any real decision. Samples are small: a decision is a direction, not proof.

### Billing, recovery and the model backend

The ledger's full contract, the offline backend example and the limits of the Claude backend are in
[brainstorm/BILLING.md](brainstorm/BILLING.md).

## Tests

```sh
python3 -m unittest discover -s evals/brainstorm/tests -t .
bash scripts/check-leaks.sh
```

Tests use fake backends, simulated subprocess responses, and actual local
processes for concurrency, crash recovery, and CLI boundaries. They never need
network access, credentials, or paid model calls. The test package points `HOME` and
`XDG_STATE_HOME` at a throwaway directory, so no test can reach the real ledger.
