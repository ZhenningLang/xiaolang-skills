# xiaolang-skills

[中文](README.zh.md)

Agent skills I use every day and think are worth sharing. I write as 小郎碎碎念 on WeChat and X.

## Skills

| Skill | What it does | Status |
|:---|:---|:---|
| [think-brainstorm](skills/think-brainstorm/README.md) | Brainstorming that spreads ideas wide before judging any of them | v1. Scored by a person on one question only. Counted against plain repeated asking on eight questions; on six new real-world questions it reached another 67 to 118 mechanisms. On the first two: it reaches mechanisms plain asking does not, 46 to 62 more on the question measured with care (Chinese method texts; the English texts shipped now reached 33 to 39 in one run). See [what the skill adds](skills/think-brainstorm/README.md#2-what-the-skill-adds) |

Each skill has its own README in its folder: what it does, when to use it, and how far it has been tested.

## What is in this repository

| Path | What it is |
|:---|:---|
| `skills/<name>/` | One skill: `SKILL.md` for the agent, `README.md` for people, its scripts and tests |
| `evals/` | Tools that measure the skills, and the result files. They are not a skill. Start at [evals/README.md](evals/README.md) |
| `docs/figures/` | Figures of the skill READMEs, with the definitions they are rendered from |
| `docs/history/` | Design notes from before v1, kept as a record. They do not describe the skills as shipped |
| `install.sh`, `.claude-plugin/` | The two ways to install |
| `AGENTS.md` | Rules for agents that change this repository |

## Install

**Claude Code**:

```bash
claude plugin marketplace add ZhenningLang/xiaolang-skills
claude plugin install think-brainstorm@xiaolang-skills
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

**brainstorm is now think-brainstorm.** If you installed it before the rename: with the plugin, run `claude plugin uninstall brainstorm@xiaolang-skills` and install `think-brainstorm@xiaolang-skills`; with `install.sh`, after `git pull` remove the old links (`rm ~/.agents/skills/brainstorm ~/.claude/skills/brainstorm`, only if they point into this repository) and run `./install.sh` again.

## License

MIT
