# xiaolang-skills

[中文](README.zh.md)

Agent skills I use every day and think are worth sharing. I write as 小郎碎碎念 on WeChat and X.

## Skills

| Skill | What it does | Status |
|:---|:---|:---|
| [brainstorm](skills/brainstorm/README.md) | Brainstorming that spreads ideas wide before judging any of them | v1. Scored by a person on one question only. Counted against plain repeated asking on two questions: it reaches mechanisms plain asking does not, 46 to 62 more on the question measured with care. See [evidence and limits](skills/brainstorm/README.md#evidence-and-limits) |

Each skill has its own README in its folder: what it does, when to use it, and how far it has been tested.

## What is in this repository

| Path | What it is |
|:---|:---|
| `skills/<name>/` | One skill: `SKILL.md` for the agent, `README.md` for people, its scripts and tests |
| `evals/` | Tools that measure the skills, and the result files. They are not a skill. Start at [evals/README.md](evals/README.md) |
| `docs/history/` | Design notes from before v1, kept as a record. They do not describe the skills as shipped |
| `install.sh`, `.claude-plugin/` | The two ways to install |
| `AGENTS.md` | Rules for agents that change this repository |

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
