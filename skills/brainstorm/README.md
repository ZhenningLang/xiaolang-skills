# brainstorm

[中文](README.zh.md)

Brainstorming that spreads ideas wide before judging any of them. This page is for people. The instructions your agent reads are in [SKILL.md](SKILL.md).

## What it does

Your coding agent sends the question to many isolated sub-agents. Each one gets a different thinking method and different material, so they do not converge on the same answers. A script groups the ideas by mechanism (what makes the idea work) and gives you every distinct mechanism. The ones plain asking already produces are folded at the end.

It only diverges. It never scores, ranks or deletes ideas; choosing is your job.

## When to use it

- Use it for an open question where you want options you have not thought of.
- Do not use it to choose between known options, to fix a bug, or for a quick list of a few ideas.
- It is expensive: one full run is about 31 idea calls plus the grouping calls.

## How to use it

Ask your agent to brainstorm a question and give it your hard constraints. The agent runs the script in `scripts/` and dispatches the sub-agents.

Thinking methods are data, not code. Four methods are the default and the only set that was evaluated. A larger set is in [methods/library.jsonl](methods/library.jsonl) (`--methods library`), and you can add a line there or pass your own file.

## Evidence and limits

How much v1 has been tested, stated plainly.

### Scored by a person: one question

- **One question, two repetitions.** How a personally maintained open-source CLI can lower the share of new users who fail on their first try.
- **Score.** The full flow scored 10 and 10; asking the model directly scored 4 and 3. Each unit had 17 ideas sampled, and the author scored them blind.
- **Where the gap comes from.** All of the difference comes from one of the three scoring questions: "I had not thought of this."
- **Blinding was incomplete.** Idea length and method-flavoured wording gave away which flow an idea came from.
- **No constraint check in the scored run.** No judge checked whether those ideas broke the question's hard constraints.
- **Unequal comparison.** The direct-ask control got no cards, and the full flow cost about 25 times as much as asking directly.
- **One setup.** Scoring was only done with Sonnet called directly, without tools. The version in this skill, where your coding agent runs its own sub-agents, has the counts below but no human score.

### Same number of calls: two questions, one run each, not scored by a person

The shipped skill ran next to plain repeated asking with no cards or methods, about 30 idea calls on each side. One grouper (Sonnet) grouped both sides. A judge of a different model then checked 25 randomly picked skill-only mechanisms for repeats under one fixed rule, and checked all mechanisms (source hidden) against the hard constraints.

| | CLI onboarding | First newsletter income |
|:---|---:|---:|
| Plain asking: calls / ideas / mechanisms | 30 / 300 / 27 | 30 / 293 / 21 |
| Skill: calls / ideas | 31 / 252 | 31 / 258 |
| Skill-only mechanisms by the grouper | 101 | 91 |
| Judged repeats in a sample of 25 | 2 | 15 |
| Skill-only after that discount (estimate) | 93 | 36 |
| Mechanisms judged to break a hard constraint | 1 of 128 | 4 of 112 |

### How far the grouping alone moves these counts

The 258 skill ideas of the second question were grouped again, with nothing else changed.

| Grouper | Skill-only mechanisms | Ideas with the same verdict in two groupings |
|:---|---:|---:|
| Sonnet, three groupings | 91, 76, 58 | 84% to 91% |
| Opus, two groupings | 76, 74 | 96% |

- **The 91 in the table above is the high end of what one grouper gives.** The same ideas gave 58 on another pass.
- **A stronger grouper is steadier.** The evaluation tool now says to group with the strongest model available and to group twice.
- **Audit of one Opus grouping.** A judge of a third model found 5 repeats in 25, which gives an estimate of 61 skill-only mechanisms. The earlier audit used another judge and found 15 in 25 (estimate 36). So on this question the skill reached roughly 36 to 61 mechanisms that plain asking did not. The same judge flagged 4 of 97 mechanisms for the weekly time limit.
- **The first question was not regrouped.** Its 101 and 93 come from one Sonnet grouping.

### What this does and does not show

- Plain asking stops finding new mechanisms early: 18 of 27 and 15 of 21 came in the first 9 calls.
- The skill reaches mechanisms plain asking did not. How many depends on the question, on the grouper and on the judge.
- Calls were equal, tokens were not: the skill's later prompts are far longer.
- The counts are of different mechanisms, not useful ones. The author read 10 skill-only mechanisms from the first question picked at random and said they would try about 7 or 8 (an overall impression, not marked item by item).
- Each judge ran one pass; a difference of one or two is noise. All flagged constraint violations were about the weekly time limit.
- The first skill run lost 2 of its 31 idea calls to a host error.
- **The larger method library showed no gain.** One run with `--methods library` gave 38 skill-only mechanisms by one Sonnet grouping (10 of 25 judged repeats). That is below the 58 to 91 that three Sonnet groupings gave the four default methods. It was grouped once, so this is not proof that the library is worse.
- **No per-step evidence.** No single step (methods, cards, rewrites, rounds, pressure levels) has its own evidence for keeping or dropping it.

Data: [evals/brainstorm/results](../../evals/brainstorm/results/). Tools: [evals/README.md](../../evals/README.md).
