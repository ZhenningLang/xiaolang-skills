# xiaolang-skills

[中文](README.zh.md)

Agent skills I use every day and think are worth sharing. I write as 小郎碎碎念 on WeChat and X.

## Skills

| Skill | What it does | Status |
|:---|:---|:---|
| [brainstorm](skills/brainstorm/SKILL.md) | Brainstorming that spreads ideas wide before judging any of them | v1, evaluated on one question only; see [Evidence and limits](#evidence-and-limits) |

Evaluation tools for these skills: [evals/README.md](evals/README.md). They measure; they are not a skill.
## Evidence and limits

How much the brainstorm v1 has been tested, stated plainly:

- **One question.** It was evaluated on a single question: how a personally maintained open-source CLI can lower the share of new users who fail on their first try. Two repetitions.
- **Score.** The full flow scored 10 and 10; asking the model directly scored 4 and 3. Each unit had 17 ideas sampled, and the author scored them blind.
- **Where the gap comes from.** All of the difference comes from one of the three scoring questions: "I had not thought of this."
- **Blinding was incomplete.** Idea length and method-flavoured wording gave away which flow an idea came from.
- **No constraint check.** No judge checked whether ideas broke the question's hard constraints.
- **Unequal comparison in the scored run.** The direct-ask control got no cards, and the full flow cost about 25 times as much as asking directly.
- **Same number of calls, one run (not scored by a person).** The shipped skill was later run once on the same question (31 idea calls, 252 ideas), next to plain repeated asking with no cards or methods (30 idea calls, 300 ideas). One grouper grouped both: plain asking reached 27 mechanisms (18 after the first 9 calls, 9 more in the next 21); the skill reached 124, of which 101 were not among the 27. A second model checked 25 of those 101 and judged 2 to be repeats. Calls were equal, tokens were not: the skill's later prompts are far longer. This counts different mechanisms, not useful ones. Two later checks on that run: a judge model read 128 mechanism summaries without knowing their source and found none that broke the question's two hard constraints (one pass, lenient when unsure); and the author read 10 of the 101 picked at random and said they would try about 7 or 8 of them (an overall impression, not marked item by item). A second question (first income for a one-person tech newsletter, same setup, no dropped calls) gave 21 mechanisms for plain asking and 109 for the skill, 91 of them skill-only by the grouper. But there the second model judged 15 of 25 sampled skill-only mechanisms to be repeats under a coarser rule (same income source, payer and way of selling), so on that question roughly 4 in 10 of the skill-only count are new at that level; the count depends on how finely mechanisms are split. It found no hard-constraint violations among 112. Data: [evals/brainstorm/results](evals/brainstorm/results/).
- **One setup was scored.** Scoring was only done with Sonnet called directly, without tools. The version in this skill, where your coding agent runs its own sub-agents, has the mechanism counts above but no human score.
- **No per-step evidence.** No single step (methods, cards, rewrites, rounds, pressure levels) has its own evidence for keeping or dropping it.

## Install

**Claude Code**:

```bash
claude plugin marketplace add ZhenningLang/xiaolang-skills
claude plugin install brainstorm@xiaolang-skills
```

**kilo, opencode, kimi code, Grok Bot, or Claude Code without plugins**:

```bash
git clone https://github.com/ZhenningLang/xiaolang-skills.git
cd xiaolang-skills
./install.sh                    # all skills into ~/.agents/skills and ~/.claude/skills
./install.sh --target agents    # only ~/.agents/skills
./install.sh --uninstall        # remove
```

`install.sh` uses symlinks by default, so `git pull` updates the skills in place. It never touches an existing folder it did not create.

## License

MIT
