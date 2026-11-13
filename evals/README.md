# Brainstorm Evaluation Tools

Measuring tools for the brainstorm skill: record ideas, merge them by mechanism,
compute machine metrics, score them blind, and decide whether a change helped.
Python 3.13 and the standard library only. File locking requires POSIX; Windows
is not supported. The brainstorm skill itself and any real exam are not here.

## Quick Start

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

## Reading The Report

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

## How Money Is Controlled

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

## Replacing A Failed Unit

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
  reports, the record stays, and neither unit can be replaced again.
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

## Where Data Lives

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

## Sheets And Pages

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

## Writing Exams

- `check_samples` must each be a **title line**, a newline, then a body. Real items look
  like that, and a one-line sample would stand out, so it is left out with a warning.
- Use descriptive names for configs, methods and models. Text that repeats such a name is
  masked on full and group sheets, and refused on comparison sheets.
- Imported units do not count as complete. Importing records their cost and ideas, but
  provides no evidence of a finished population for metrics, comparison or replay.

## Not Verified

- How real models format output and rewrites: code fences, kept title lines and uniform
  style. A single smoke run is an observation, not a guarantee.
- Rewrite quality. The fake rewrite proves wiring only.
- Backends other than Claude, such as a generic command backend pointing at another tool.
  Those paths have only been tested against local fake processes.
- Mechanism grouping on real text. The fake classifier matches identical text only.
- Real calls for calibration, constraint checks, replay, methods and pressure levels. The
  one real smoke run covered one agent, the baseline and first round, grouping, and one
  rewritten item.
- Running a real backend from inside an agent session. The adapter passes its whole
  environment to the subprocess, including that session's variables. The smoke run removed
  them by hand first, so this path has never run as it stands.
- Bigram TF-IDF is a rough lexical estimate, not semantic coverage.
- Any real decision. Samples are small: a decision is a direction, not proof.
- Replacement and the removal comparison on a real backend. Both have only run on the fake one.

## Offline Backend Example

```python
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger, guarded_call

ledger = BudgetLedger("evals/brainstorm/runs/example/ledger.fake.jsonl", limit="50")
result = guarded_call(ledger, FakeBackend(), "example_attempt_1", "Say hello", "test", 20)
print(result.text)
```

Always route model calls through `guarded_call`. A call key identifies the exact
request (exam, configuration, repetition, agent, batch, prompt hash, model,
attempt). Reuse a key only for the same request; retry a paid failure with a new
attempt. The caller owns that identity contract, not this billing layer.

`Backend` supplies `name`, `estimate_max_usd(prompt, model, max_output_tokens)`,
and `call(prompt, model, max_output_tokens)`. `CallResult` contains `text`, `usd`,
`tokens` (`input`, `cache_write`, `cache_read`, `output`), `raw_ref`, and `outcome`.
Use `Decimal` or decimal strings for configured amounts. FakeBackend accepts
`usd`, `reserve_usd`, and `failure` (`not_started`, `paid_error`, `unknown`, or
`missing_cost`); setting `usd > reserve_usd` simulates an overrun.

## Billing And Recovery

The default limit is USD 50. Reservations are checked and persisted under a
stable file lock before starting a backend. Used budget is settled cost plus
pending reservations and uncertain calls, counted once per key. A reservation
equal to the remaining budget is allowed; a larger one never starts the backend.
Money is written as JSON numbers and accounted for with decimal arithmetic.
Call reservations must be positive; configure a conservative model price rather
than treating unknown charges as free. Amounts outside the supported Decimal
exponent range are rejected before changing the ledger.

Success writes an atomic cache before settlement. Cache hits cost nothing.
An error with known cost is settled as `paid_error`, without a success cache.
If result validation or cache persistence fails while the ledger remains writable,
a known charge is settled as `paid_error`; an unknown charge remains uncertain.
If settlement cannot be persisted, the process permanently disables all guarded
calls, including calls through new ledger objects or a different ledger path.
The error includes the call key, reserved amount, actual amount, and delta.
An actual overrun uses exit code 3 even when persistence fails; other persistence
failures use exit code 1. Callers must exit, not catch the error and continue.

On a transaction write failure, while still holding the settlement lock, the process tries to fsync billing
evidence into that existing lock file. If this succeeds, other processes also
stop before admitting new reservations. No additional persistence layer is used.
If both ledger and lock writes fail, evidence is available only in the error;
cross-process or post-restart stopping cannot be guaranteed. Do not treat the
old reservation as the actual charge after storage recovers. Preserve the error,
ledger, cache, and lock; stop all clients and reconcile the missing charge before
any restart. There is no automatic unlock/recovery command: a halted ledger's
status, settle, and release commands currently refuse access. Recovery policy
requires an operator decision, not erasing evidence or starting an empty ledger.
Automatic settlement handles its own call only; it does not recover unrelated
dead owners. Reserve, status, and manual reconciliation retain that recovery.
Failures to acquire or read the ledger before the billing transaction can stop
only the current process, without persistent lock evidence. Preserve their error
diagnostics as well; this is not a cross-process guarantee after storage recovery.
A successful response without cost settles its full reservation as `estimated`;
status reports this separately. An unreadable result keeps its reservation as
`uncertain`, not as zero. Only a definitely unstarted request is automatically
released. The adapter never retries model requests.

A dead owner's cached result repairs its missing settlement. A dead owner
without a cache becomes uncertain and cannot be resent. Corrupt caches or ledger
records fail closed. Process ownership uses POSIX PID liveness; do not copy an
active ledger between machines or manually edit it. Empty existing ledgers and
missing ledgers with surviving caches are corruption, not fresh budgets. Ledger events are logically
append-only: atomic replacement preserves the complete previous prefix. Locks
and caches are adjacent to the resolved ledger path, so multiple run directories
using one ledger also share recovery and exclusion.

If actual cost exceeds its reservation, the actual amount is still recorded,
then **exit code 3** is used at the CLI boundary. The diagnostic includes
reserved, actual, and delta. All new calls on that ledger stop, even if total
spending is below its limit; restarting or increasing the limit does not clear
this condition. Existing successful caches can still be read. This protects
subsequent calls, but a **single call** can exceed its reservation and budget.

`default_ledger_path(backend, run_dir)` selects `run_dir/ledger.fake.jsonl` for
fake calls and the state-directory path above for all real backends,
independent of run directory. An explicit ledger path overrides this selection;
do not use a new empty real ledger to evade cumulative costs. Private exams and
run outputs are ignored by Git. Do not publish raw model responses or caches.

```sh
python3 -m evals.brainstorm --help
python3 -m evals.brainstorm ledger --ledger path/to/ledger.jsonl status
python3 -m evals.brainstorm ledger --ledger path/to/ledger.jsonl settle CALL_KEY --usd 0.01
python3 -m evals.brainstorm ledger --ledger path/to/ledger.jsonl release CALL_KEY --confirm-not-charged
```

Status prints JSON: `used`, `settled`, `reserved`, `uncertain`, `estimated`,
`remaining`, `limit`, and `overruns`. Manual reconciliation refuses live owners.
Repeating an identical settlement or release is idempotent. Settlement without a
cached response reconciles billing only; it does not fabricate a usable result.
Release requires independent evidence that no charge occurred. Settled calls
cannot be released or given a different cost. Other errors use exit code 1.
Zero-charge manual reconciliation must use the confirmed release command, not
`settle --usd 0`. Limits remain caller-configured; pass the same limit to every
client and status command sharing a ledger.

## Claude Boundary

`ClaudeCliBackend(pricing=...)` invokes `claude -p` with the prompt on stdin,
empty setting sources and tools, strict MCP configuration, disabled commands,
JSON output, and no session persistence. It parses cost even on a nonzero exit.
Only a failure to construct the subprocess counts as unstarted; communication
errors after startup retain the reservation. Missing usage fields are not
fabricated as zero tokens: the result is an unusable paid error, with its known
charge preserved. It never uses shell interpolation. Tests do not invoke a real model.

Pricing is required for each model; there is no unknown-model fallback:

```python
pricing = {"your_model": {"fixed_tokens": 10000,
                          "input_per_million": "CONFIGURE_CURRENT_RATE",
                          "output_per_million": "CONFIGURE_CURRENT_RATE"}}
```

These are configuration placeholders, not quoted prices. The estimate is
`((fixed_tokens + UTF-8 prompt bytes) * input_per_million
+ max_output_tokens * output_per_million) / 1000000`. Configure fixed overhead
and conservative rates to cover hidden context and cache-write pricing.

**Output cap boundary:** the subprocess environment explicitly sets
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` to `max_output_tokens`, overriding any inherited
value without changing the parent environment. CLI version 2.1.316 contains the
environment-variable reader and corresponding output-limit diagnostics. The
official reference is <https://code.claude.com/docs/en/env-vars>; the docs page
could not be fetched during this implementation round. This setting is not a
proven universal hard cap, nor is the CLI budget option a reliable pre-call hard
limit. The adapter does not retry requests; the vendor CLI's internal retry
behavior is outside that guarantee. Personal configuration or automatically loaded
context may affect the prompt despite the isolation flags; its absence remains
unverified. Real cost fidelity and personal-context contamination require a
separate explicitly authorized, budgeted smoke test. No real smoke is part of
the automated suite, and a reservation is not a vendor-enforced spending cap.

## Testing The Shipped Skill

Everything above measures the flow through direct model calls. `skill_budget.py` measures the shipped
skill itself, where a host agent dispatches sub-agents. It calls no model: the host dispatches the tasks
it prints, the same way it does for the skill.

**Smoke check, before a release.** Run the skill once on any exam question, then:

```sh
python3 -m evals.brainstorm.skill_budget check --dir <skill run dir>
```

It fails if the run is unfinished, an idea agent was dropped, ideas were left ungrouped, a level is
missing, or a level added fewer than `--min-new` mechanisms beyond the baseline.

**Same number of calls, plain asking.** The control asks the same question again and again with no
cards, no methods and no rounds, for as many idea calls as the skill run used:

```sh
python3 -m evals.brainstorm.skill_budget direct-init --dir <control dir> --question-file q.txt --batches 10
python3 -m evals.brainstorm.skill_budget next --dir <control dir>      # dispatch, repeat until done
cp -R <control dir> <second control dir>                               # for `repeat`, below
python3 -m evals.brainstorm.skill_budget merge --dir <control dir> --from <skill run dir>
python3 -m evals.brainstorm.skill_budget next --dir <control dir>      # dispatch, repeat until done
python3 -m evals.brainstorm.skill_budget compare --dir <control dir>
```

**Audit of the comparison.** The grouper may split one mechanism into several, which inflates the
skill-only count. After the merge:

```bash
python3 -m evals.brainstorm.skill_budget audit-init --dir <control dir> --sample 25
# give the printed prompt file to one judge agent of a different model than the grouper
python3 -m evals.brainstorm.skill_budget audit-read --dir <control dir>
```

`audit-init` writes one prompt with two parts. Part 1 lists every mechanism plain asking reached and a
random sample of skill-only mechanisms; the judge names repeats under one fixed rule (`SAME_RULE` in the
script, the same for every question). Part 2 lists all mechanisms in random order with the source hidden;
the judge names the hard constraints each one breaks. `audit-read` refuses a partial answer and reports
the repeats, the discounted skill-only estimate, and violations by side. The judge runs one pass: treat
a difference of one or two as noise.

**How far the grouping alone moves the counts.** Group with the strongest model you have, and group
twice. Copy the finished control run before the merge, merge the same skill run into each copy, then:

```bash
python3 -m evals.brainstorm.skill_budget repeat --dir <control dir> --dir <second control dir>
```

`repeat` reports the skill-only count of each grouping and its range, and for each idea of the skill
run whether the groupings agree that plain asking did or did not reach it. Report the range, not one
count. Measured once on 258 ideas: three Sonnet groupings gave 58, 76 and 91 skill-only mechanisms
(84% to 91% of ideas with the same verdict); two Opus groupings gave 74 and 76 (96%).

`merge` puts every idea of the skill run through the control run's grouper, so both sides are counted
by one grouping. `compare` reports the mechanisms only the skill reached, only the control reached, and
both, plus how fast the control stops finding new mechanisms.

Limits: calls are equal, tokens are not (the skill's later prompts are far longer). The control ideas are
grouped first, so a borderline skill idea is more likely to be merged into a control mechanism than the
other way round. Mechanism counts depend on the grouping model. None of this measures usefulness.

## Tests

```sh
python3 -m unittest discover -s evals/brainstorm/tests -t .
bash scripts/check-leaks.sh
```

Tests use fake backends, simulated subprocess responses, and actual local
processes for concurrency, crash recovery, and CLI boundaries. They never need
network access, credentials, or paid model calls. The test package points `HOME` and
`XDG_STATE_HOME` at a throwaway directory, so no test can reach the real ledger.
