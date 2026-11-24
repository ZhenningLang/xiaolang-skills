---
name: think-compare
description: Use when the candidates (options, tools, approaches, paths or decisions) are already mostly known and the user needs to choose between them ("A or B", "which should we pick", "compare X, Y and Z"). Defines the dimensions that matter for this question first, compares the candidates dimension by dimension with a winner per dimension, and ends with an explicit choice, including a conditional choice or a tie with what would break it. Not for finding candidates that are not known yet or judging feasibility (research first, then compare), and not for an open topic where the user wants an overview without a verdict.
license: MIT
metadata:
  version: "1.0"
---

# Compare

Turn a "which one" question from a gut call into a comparison along dimensions. The point is not to take a side first; it is to ask first: which dimensions should this question be compared on?

## 1. Pin down the candidates

Read the user's request and list the candidates first:

- If the candidates are clear, use the ones the user gave.
- If the list looks incomplete, add one or two reasonable alternatives and mark them `[inferred]`.
- If the question is not a choice yet, rewrite it as a choice between candidates, then go on.
- If the candidates are not settled yet and finding them needs research, research them first, then compare.
- If key information is missing so badly that no comparison is possible, ask the fewest clarifying questions that unblock it.

## 2. Define the dimensions before comparing

Do not compare the candidates yet. From the nature of the question, list 4 to 8 dimensions and say why each one matters here.

Common dimensions:

| Kind of question | Dimensions to consider |
|---|---|
| Technology choice | Fit with the existing ecosystem, complexity, maintainability, performance, testability, migration cost, long-term risk |
| Implementation path | Delivery speed, risk, ease of rollback, cost of verification, blast radius, team familiarity |
| Architecture | Clarity of boundaries, extensibility, data consistency, fault isolation, observability, operating cost |
| Product or strategy | Value to users, opportunity cost, reversibility, learning value, cost of execution, time sensitivity |

The dimensions must come from this question; do not apply the table mechanically. Each dimension must separate the candidates; drop any that does not.

## 3. Compare dimension by dimension

Use this table:

```md
| Dimension | Why it matters | Option A | Option B | Option C | Winner |
|---|---|---|---|---|---|
| ... | ... | strength / weakness | strength / weakness | strength / weakness | A / B / C / tie |
```

Rules:

- Each cell states a difference someone can check, not a vague adjective.
- A winner can be "tie", but say why there is no clear gap.
- If a dimension rests on a premise that has not been checked, mark it `[unverified]`.
- You may weight the important dimensions, but state each weight and why.

## 4. Make the choice

End with an explicit verdict:

```md
## Verdict

Recommended: Option X / tie

Why:
1. ...
2. ...
3. ...

Holds when:
- ...

If that changes:
- When ..., choose Option Y instead
```

If the result is a tie, say:

- which dimensions are tied;
- which one piece of information would break the tie;
- if a choice must be made now, which option is the safer default.

## Gotchas

- Do not reach a conclusion first and pick dimensions to fit it.
- Do not stop at a list of pros and cons; compare dimension by dimension and name a winner for each.
- Do not weigh every dimension equally; a hard constraint outweighs a minor preference.
- Do not hide behind "it depends"; give a conditional verdict or a tie verdict instead.
- Do not mix facts, inferences and preferences; mark anything not verified.
- Answer in the language the user writes in.
