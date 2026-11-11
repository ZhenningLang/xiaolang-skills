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
- **Unequal comparison.** The direct-ask control got no cards. The full flow cost about 25 times as much as asking directly, and there was no equal-budget comparison.
- **One setup.** It was only tested with Sonnet called directly, without tools. The version in this skill, where your coding agent runs its own sub-agents, has not been scored.
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
