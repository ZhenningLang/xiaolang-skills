# brainstorm

[中文](README.zh.md)

Brainstorming that spreads ideas wide before judging any of them. This page is for people. The instructions your agent reads are in [SKILL.md](SKILL.md).

## What it does

Your coding agent sends the question to many isolated sub-agents. Each one gets a different thinking method and different material, so they do not converge on the same answers. A script groups the ideas by mechanism (what makes the idea work) and gives you every distinct mechanism. The ones that the run's own first stage already produced, with no thinking method, are folded at the end.

It only diverges. It never scores, ranks or deletes ideas; choosing is your job.

## When to use it

- Use it for an open question where you want options you have not thought of.
- Do not use it to choose between known options, to fix a bug, or for a quick list of a few ideas.
- It is expensive: the second measured run below was 31 idea calls and 12 grouping calls, about 43 sub-agent calls. Tokens and money were not recorded for the shipped skill. The script stops and asks before the most expensive part.

## How to use it

Ask your agent to brainstorm a question and give it your hard constraints (things no idea may break). If you have notes or source material, hand them over: the agent cuts them into cards and each sub-agent gets a random half. The agent runs the script in `scripts/` and dispatches the sub-agents.

You get `report.md`: a contents list with one line per mechanism, then every mechanism with all the ideas grouped under it and where each idea came from.

Two profiles:

- **full** (default): every stage in the table below. This is the flow that was counted against plain asking.
- **economy**: stops after round 2. Cheaper; not measured on its own.

## How one run works

| Stage | Idea calls | What a sub-agent is told | What it can see of earlier ideas |
|:---|---:|:---|:---|
| Baseline | 9 | The question, constraints and cards. No method. | Nothing |
| Round 1 | 3 | The same, plus one thinking method each | Nothing |
| Round 2 | 3 | The same, plus: explore the parts of the question not yet covered | One idea per mechanism found so far, to avoid; the ways earlier agents split the question; titles of the ideas that stand alone in their mechanism, as starting points |
| Pressure 1 | 3 | Avoid every mechanism found so far | The same three things, now longer |
| Pressure 2 | 3 | Prefer cards no agent has received yet | The same |
| Pressure 3 | 3 | Question the premises of the question | The same, plus how often each way of splitting was used |

- **The first two stages are blind on purpose.** They give several starting points that did not influence each other.
- **From round 2 on, earlier ideas are shown as things to avoid**, not as examples to follow.
- **Ideas that repeat a baseline mechanism go back once.** In every stage after the baseline, a fresh sub-agent with the same method and cards gets such ideas and the avoid list, and rewrites them. These rewrites are the calls beyond the 24 in the table: up to 3 per stage, and 7 in the second measured run.
- **Pressure 2 seldom has a new card to give.** The nine baseline agents each get half the cards, so with ten cards every card is normally taken before pressure 2. Its agents then get cards that were used before.
- **Grouping.** A sub-agent groups the new ideas by mechanism after each batch (three times in the baseline) and again after the rewrites of a stage: 12 grouping calls in the second measured run. Grouping only merges; every idea stays visible in the report.

Thinking methods are data, not code. Four methods are the default. A larger set of 18 is in [methods/library.jsonl](methods/library.jsonl) (`--methods library`); you can add a line there or pass your own file. The larger set has shown no gain so far (see below).

## Evidence and limits

### Words used here

- **Mechanism**: what makes an idea work. Two ideas with the same mechanism count once.
- **Baseline**: the first stage of a skill run (see the table above). It has cards and no method. Your report folds what it produced.
- **Plain asking**: a separate comparison run, made only for measuring. The same question asked again and again with no cards, no methods and no rounds, for the same number of idea calls as the skill. It is not the baseline.
- **Full flow** and **asking directly**: the two sides of the person-scored test, both run through direct model calls before the skill was packaged. The full flow has the stages of the table above. Asking directly is nine model calls with the bare question: no cards, no methods, no rounds.
- **Grouper**: the model that sorts ideas into mechanisms. In a comparison, the plain-asking ideas are grouped first; the skill's ideas are then put into those mechanisms or into new ones.
- **Skill-only mechanism**: a mechanism that holds ideas from the skill run and none from plain asking.
- **Beyond the baseline**: the count your own run reports. It compares with that run's baseline, so it is not the skill-only count. It was higher in both measured runs (112 and 107).
- **Judge**: a model of another kind than the grouper. It reads the skill-only mechanisms and names those that repeat a plain-asking mechanism. It also checks hard constraints.
- **Pass**: one complete reading by the judge.

### In short

- **Scored by a person on one question only.** Of 17 mechanisms sampled per repetition, the author counted 10 and 10 as new and worth caring about for the full flow, against 4 and 3 for asking the model directly.
- **It reaches mechanisms that plain repeated asking does not.** With the same number of idea calls, on the one question measured with care, between 46 and 62 of them.
- **That number is a count of different mechanisms, not of useful ones.**
- **The larger method library shows no evidence of a gain.** The default stays at four methods.
- **No single step has its own evidence.** Methods, cards, rewrites, rounds and pressure stages were only measured together.

### The measurements at a glance

R = [evals/brainstorm/results](../../evals/brainstorm/results/), where each file is described in a README.

| Date | Question | What was compared | How it was measured | File in R |
|:---|:---|:---|:---|:---|
| 11-08 | CLI onboarding | The full flow against asking directly, before the skill was packaged | The author scored 17 mechanisms per repetition, blind | `2026-11-08-human-scored-cli.json` |
| 11-12 | CLI onboarding | Shipped skill, plain asking | Sonnet grouping once; judge on 25 | `2026-11-12-equal-calls-cli.json` |
| 11-13 | First newsletter income | Shipped skill, plain asking | Sonnet grouping once; judge on 25. The two runs are still the ones used; the count and the judge's result are replaced by the 11-14 rows | `2026-11-13-equal-calls-side-income.json` |
| 11-13 | First newsletter income | Skill with the library of 18 methods | Sonnet grouping once. Replaced by the last row | `2026-11-13-method-library-side-income.json` |
| 11-14 | First newsletter income | The same skill run, grouped again | Sonnet three times, Opus twice | `2026-11-14-grouper-repeat-side-income.json` |
| 11-14 | First newsletter income | 4 methods, library of 18 | Opus grouping twice; judge on every mechanism, twice | `2026-11-14-methods-default-vs-library-side-income.json` |

### Scored by a person: one question

The "full" flow against asking directly, before the skill was packaged. Both called Sonnet directly, without tools.

| | Asking directly | Full flow |
|:---|---:|---:|
| Idea calls per repetition | 9 | 24, plus 4 and 7 rewrites |
| Ideas per repetition | 90, 90 | 233, 247 |
| Mechanisms per repetition | 21, 23 | 123, 133 |
| Mechanisms shown to the author, per repetition | 17 | 17 |
| **Score, two repetitions** | **4, 3** | **10, 10** |
| "I had not thought of this", of 17 | 4, 3 | 10, 11 |
| "I care about this", of 17 | 16, 16 | 16, 15 |
| Cost of idea generation, two repetitions together | USD 0.25 | USD 7.03 |

- **The question.** How a personally maintained open-source CLI can lower the share of new users who fail on their first try.
- **How it was scored.** For each repetition, 17 of its mechanisms were picked by one seeded order. Each was shown as one idea, rewritten in plain words, with nothing on the page about its source. The author answered every item before any source was shown. A mechanism scores when the author had not thought of it, cares about it and gives no fatal reason.
- **Where the gap comes from.** All of it comes from "I had not thought of this". "I care about this" is equal on both sides.
- **The 17 are a small part of the full flow.** They cover 17 of 21 or 23 mechanisms of asking directly, and 17 of 123 or 133 of the full flow. So the score compares what share of a sample was new to the author, not how many new mechanisms each side found.
- **The repetitions are not independent.** The two direct repetitions share 13 of their 17 mechanisms; a shared mechanism was scored once and counted for both.
- **Blinding was incomplete.** By the author's account, idea length and method-flavoured wording gave away which flow an idea came from.
- **No constraint check.** No judge checked whether those ideas broke the question's hard constraints.
- **Unequal comparison.** Asking directly got no cards, and the full flow cost about 28 times as much (22 and 36 times in the two repetitions), before the cost of grouping.
- **One setup, and not the shipped one.** The scored flow called the model directly. The skill, where your coding agent runs its own sub-agents with the same prompts and the same 24 idea calls, has the counts below but no human score.

### Counted against plain asking: two questions, one run each

The shipped skill ran next to plain asking, about 30 idea calls on each side. All idea agents on both sides were Sonnet sub-agents of a coding agent. No person scored these runs.

| | CLI onboarding | First newsletter income |
|:---|---:|---:|
| Plain asking: calls / ideas / mechanisms | 30 / 300 / 27 | 30 / 293 / 21 |
| Skill: calls / ideas | 31 / 252 | 31 / 258 |
| Grouper of the plain-asking ideas | Sonnet | Sonnet |
| Grouper of the skill's ideas | Sonnet, once | Opus, twice |
| Skill-only mechanisms by the grouper | 101 | 76 and 74 |
| Judge | Opus | a third Claude model |
| Checked by the judge | 25 picked at random, once | all of them, twice per grouping |
| Skill-only mechanisms left after the judge | about 93 (estimate) | 53 and 62 (first grouping), 51 and 46 (second) |
| Mechanisms judged to break a hard constraint | 1 of 128 | 0 to 4 of 95 to 97, in five passes |

- **The second question is the careful measurement.** Its range of 46 to 62 comes from two groupings and four judge passes.
- **The first question was measured the early way and not redone.** One Sonnet grouping and a sample of 25 are both known to be unsteady (see the next section), so read 101 and 93 as rough.
- **Plain asking slows down early but does not stop.** 18 of 27 and 15 of 21 mechanisms came in the first 9 calls. On both questions calls 25 to 27 still added 3.
- **All constraint violations were of one kind**: over the weekly time limit the question set. This was read from the judges' answers; the result files hold only the counts.
- **The constraint check was not blind.** The judge got no source label, but could work out the source from the other half of its prompt.
- **Calls were equal, tokens were not.** The skill's later prompts are far longer.
- **One read by the author.** The author read 10 skill-only mechanisms of the first question picked at random and said they would try about 7 or 8 (an overall impression, not marked item by item).
- **The first skill run lost 2 of its 31 idea calls** (two rewrite tasks) to a host error.
- **One family of models.** Idea agents, groupers and judges were all Claude models.

### How steady the measuring tools are

Measured on the second question, with the ideas unchanged.

| What was repeated | Result |
|:---|:---|
| Sonnet grouping, three times | 91, 76 and 58 skill-only mechanisms; 84% to 91% of ideas got the same verdict |
| Opus grouping, twice | 76 and 74; 96% of ideas got the same verdict |
| Judge on a sample of 25 from each of two groupings (the two agree on 96% of ideas) | 5 and 16 repeats |
| Judge on every mechanism of the default run, twice | Same verdict on 67 of 76, and on 65 of 74 |
| Judge on every mechanism of the library run, twice | 17 and then 60 of 99 called repeats |

- **A stronger grouper is steadier.** The evaluation now groups with the strongest model available, twice.
- **A sample of 25 is too small.** The evaluation now checks every skill-only mechanism, twice.
- **The judge is the least steady part.** A difference between two runs counts only when it is larger than the difference between two judge passes over one run.

### Four methods against the library of 18

One run with `--methods library` on the second question, compared with the default run. Both were grouped twice by Opus against the same plain-asking run, and the judge checked every skill-only mechanism twice per grouping.

| | 4 methods (default) | Library of 18 |
|:---|---:|---:|
| Idea calls | 31 | 29 |
| Methods used | 4 of 4 | 15 of 18 |
| Skill ideas | 258 | 246 |
| Skill-only mechanisms by the grouper, two groupings | 76, 74 | 99, 109 |
| Left after the judge removed repeats, four passes | 53, 62, 51, 46 | 82, 39, 31, 38 |

- **The grouper counts more for the library, the judge removes more.** Three of four judge passes leave the library below the default methods; one leaves it above.
- **So there is no evidence of a gain, and no proof of a loss.** The default stays at four methods.
- **One run on each side.** How much a second skill run would differ was not measured.

### Not known

- Whether the result holds on other questions. Two were counted, one was scored.
- How much two runs of the skill on one question differ.
- Which steps earn their cost. Nothing was removed and re-measured.
- Whether the mechanisms beyond plain asking are useful. Only the one informal read above.
- How the skill does with no cards, or with cards the agent gathers itself. Every measured run used 10 cards the author wrote in advance.
- How it does on other models. Idea agents, grouper and judge were all Claude models.

### Earlier numbers you may meet

Result files and older commits hold numbers that later measurements replaced.

| Number | What it was | Replaced by |
|:---|:---|:---|
| 91 skill-only mechanisms on the second question | One Sonnet grouping, the high end of three | 76 and 74 (Opus, twice) |
| 36, and "36 to 61" | Estimates from samples of 25 | 46 to 62 (every mechanism checked) |
| 38 for the method library | One Sonnet grouping, a low outlier | 99 and 109 by the grouper; 31 to 82 after the judge |

Data: [evals/brainstorm/results](../../evals/brainstorm/results/). Tools and how to repeat the comparison: [evals/README.md](../../evals/README.md).
