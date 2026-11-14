# Results

Summaries of every measurement of the brainstorm skill. Each file holds counts and descriptions only: no idea text, no model output. The raw runs are not in the repository.

What the numbers mean, and their limits, is in [skills/brainstorm/README.md](../../../skills/brainstorm/README.md#evidence-and-limits). How to repeat a comparison is in [evals/README.md](../../README.md).

## Files, oldest first

| File | Question it answers | Still current? |
|:---|:---|:---|
| `2026-11-08-human-scored-cli.json` | Does the author find more new ideas in the full flow than in asking the model directly? One question, two repetitions, blind scoring | Yes. The only human score |
| `2026-11-12-equal-calls-cli.json` | With equal idea calls, does the shipped skill reach mechanisms plain asking does not? First question | Yes, but measured the early way: one Sonnet grouping, judge on a sample of 25 |
| `2026-11-13-equal-calls-side-income.json` | The same, second question | Its `compare.control` (the plain-asking run) is current. Its skill-only count of 91 and both audits are replaced by the two 11-14 files |
| `2026-11-13-method-library-side-income.json` | Does the library of 18 methods reach more than the four default methods? | No. Its count of 38 is a low outlier of one Sonnet grouping. Replaced by the last file |
| `2026-11-14-grouper-repeat-side-income.json` | How far does the grouping alone move the count? Three Sonnet groupings and two Opus groupings of one skill run | Yes |
| `2026-11-14-methods-default-vs-library-side-income.json` | Four methods against the library, grouped twice by Opus, every skill-only mechanism judged twice | Yes. Also the current numbers for the default skill on the second question |

## Fields that look contradictory

- **Two audits in `2026-11-13-equal-calls-side-income.json`.** `audit` used a rule written for that one question. `audit_fixed_rule` used the rule that every later audit uses (`SAME_RULE` in `skill_budget.py`). Both found 15 repeats in 25. Read `audit_fixed_rule`; `audit` is kept as a record.
- **`hard_constraints.violations` is 0 and `audit_fixed_rule` reports 4 in the same file.** They are two judges on two dates. Neither is the current number: the skill README quotes the audits of the two 11-14 files (0 to 4 of 95 to 97 in five passes).
- **`skill.mechanisms` and `beyond_its_own_baseline` are not the comparison.** They come from the skill run's own grouping and its own baseline. The comparison is `same_grouper`, where the skill's ideas were grouped again together with the plain-asking ideas.
- **`2026-11-12-equal-calls-cli.json` has no top-level date.** Its date and setup are under `notes`.

## Known flaws in the data

- In `2026-11-13-equal-calls-side-income.json` and in grouping `opus-2`, one idea each was left ungrouped because the grouper wrote a wrong idea id. It counts as its own mechanism.
- The first skill run (`2026-11-12`) lost two rewrite tasks to a host error.
- The library run made 29 idea calls, the default run 31, and the library run used 15 of its 18 methods.
- In the human-scored run, one call ended with unknown billing, was recorded as not charged and was retried. If it was charged, the cost of the full flow is understated by that one call.
- No file records which hard constraint a flagged mechanism broke.
