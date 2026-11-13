# xiaolang-skills

[中文](README.zh.md)

Agent skills I use every day and think are worth sharing. I write as 小郎碎碎念 on WeChat and X.

## Skills

| Skill | What it does | Status |
|:---|:---|:---|
| [brainstorm](skills/brainstorm/README.md) | Brainstorming that spreads ideas wide before judging any of them | v1. Scored by a person on one question only; reaches mechanisms plain asking does not, how many is uncertain. See [evidence and limits](skills/brainstorm/README.md#evidence-and-limits) |

Each skill has its own README in its folder: what it does, when to use it, and how far it has been tested.

Evaluation tools for these skills: [evals/README.md](evals/README.md). They measure; they are not a skill.

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
