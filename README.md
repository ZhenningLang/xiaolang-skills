# xiaolang-skills

[中文](README.zh.md)

Agent skills I use every day and think are worth sharing. I write as 小郎碎碎念 on WeChat and X.

## Skills

| Skill | What it does | Status |
|:---|:---|:---|
| [brainstorm](skills/brainstorm/SKILL.md) | Brainstorming that spreads ideas wide before judging any of them | In design, not usable yet |

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
