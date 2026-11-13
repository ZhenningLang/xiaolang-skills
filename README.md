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
- **Same number of calls, two questions, one run each (not scored by a person).** The shipped skill ran next to plain repeated asking with no cards or methods, about 30 idea calls on each side, and one grouper grouped both sides. A judge of a different model then checked, with one fixed rule, 25 randomly picked skill-only mechanisms for repeats, and all mechanisms (source hidden) against the hard constraints.

  | | CLI onboarding | First newsletter income |
  |:---|---:|---:|
  | Plain asking: calls / ideas / mechanisms | 30 / 300 / 27 | 30 / 293 / 21 |
  | Skill: calls / ideas / mechanisms | 31 / 252 / 124 | 31 / 258 / 109 |
  | Skill-only mechanisms by the grouper | 101 | 91 |
  | Judged repeats in a sample of 25 | 2 | 15 |
  | Skill-only after that discount (estimate) | 93 | 36 |
  | Mechanisms judged to break a hard constraint | 1 of 128 | 4 of 112 |

  What this does and does not show: plain asking stops finding new mechanisms early (18 of 27 and 15 of 21 came in the first 9 calls). The skill reaches mechanisms plain asking did not, but how many depends on the question and on how finely mechanisms are split: on the second question most skill-only groups were another way of doing something plain asking had already found. Calls were equal, tokens were not: the skill's later prompts are far longer. The counts are of different mechanisms, not useful ones. The judge ran one pass; an earlier one-off pass found no constraint violations, so one or two either way is noise. All flagged violations were about the weekly time limit. The author read 10 skill-only mechanisms from the first question picked at random and said they would try about 7 or 8 (an overall impression, not marked item by item). The first skill run lost 2 of its 31 idea calls to a host error. Data: [evals/brainstorm/results](evals/brainstorm/results/).
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
