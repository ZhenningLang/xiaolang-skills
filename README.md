# xiaolang-skills

[中文](README.zh.md)

Agent skills I use every day and think are worth sharing. I write as 小郎碎碎念 on WeChat and X.

## Skills

| Skill | What it does | Status |
|:---|:---|:---|
| [think-brainstorm](skills/think-brainstorm/README.md) | Brainstorming that spreads ideas wide before judging any of them | v1. Scored by a person on one question only. Counted against plain repeated asking on eight questions; on six new real-world questions it reached another 67 to 118 mechanisms. On the first two: it reaches mechanisms plain asking does not, 46 to 62 more on the question measured with care (Chinese method texts; the English texts shipped now reached 33 to 39 in one run). See [what the skill adds](skills/think-brainstorm/README.md#2-what-the-skill-adds) |
| [think-compare](skills/think-compare/README.md) | Choose between options you already have: dimensions first, a winner per dimension, then one verdict with its conditions | v1. Against plain asking on eight technology-selection questions, judged blind in both orders by a model from another company: preferred in 11 of 16 judgments, in both orders on 5 of 8 questions (plain asking on 2). Mean score 10.875 against 10.5 of 12, mostly from stating conditions and checkable differences. See [evidence](skills/think-compare/README.md#3-evidence) |

## Tools

Commands you run yourself; no agent decides to use them.

| Tool | What it does | Status |
|:---|:---|:---|
| [git-tidy](tools/git-tidy/README.md) | Cleans up finished worktrees and branches, deleting only what cannot lose work | v1. On 12 built scenarios it deleted nothing that should stay and kept nothing that could go; `git branch --merged` would delete 6 that should stay and miss 2. See [evidence](tools/git-tidy/README.md#3-evidence) |

Each skill and tool has its own README in its folder: what it does, when to use it, and how far it has been tested.

## What is in this repository

| Path | What it is |
|:---|:---|
| `skills/<name>/` | One skill: `SKILL.md` for the agent, `README.md` for people, its scripts and tests |
| `tools/<name>/` | One tool: its command-line program in `scripts/`, an optional slash command in `commands/`, `README.md` for people, its tests |
| `evals/` | Programs that measure the skills and tools, and the result files. Start at [evals/README.md](evals/README.md) |
| `docs/figures/` | Figures of the skill READMEs, with the definitions they are rendered from |
| `docs/history/` | Design notes from before v1, kept as a record. They do not describe the skills as shipped |
| `install.sh`, `.claude-plugin/` | The two ways to install |
| `AGENTS.md` | Rules for agents that change this repository |

## Install

**Claude Code**:

```bash
claude plugin marketplace add ZhenningLang/xiaolang-skills
claude plugin install think-brainstorm@xiaolang-skills
claude plugin install think-compare@xiaolang-skills
claude plugin install git-tidy@xiaolang-skills   # the /git-tidy command; the git tidy program comes from install.sh below
```

**kilo, opencode, kimi code, Grok Bot, or Claude Code without plugins**:

```bash
git clone https://github.com/ZhenningLang/xiaolang-skills.git
cd xiaolang-skills
./install.sh                    # all skills into ~/.agents/skills and ~/.claude/skills, tools into ~/.local/bin
./install.sh --target agents    # only ~/.agents/skills
./install.sh git-tidy           # only the git tidy program
./install.sh --uninstall        # remove
```

`install.sh` uses symlinks by default, so `git pull` updates the skills and tools in place. It never touches an existing file or folder it did not create. Put `~/.local/bin` on your `PATH` so that `git tidy` is found.

**git-tidy is now a tool, not a skill.** Agents no longer start it on their own: run `git tidy plan` / `git tidy apply`, or `/git-tidy` to have an agent run it and report. If you installed the git-tidy skill with `install.sh`, remove the old links (`rm ~/.agents/skills/git-tidy ~/.claude/skills/git-tidy`, only if they point into this repository) and run `./install.sh git-tidy`.

**brainstorm is now think-brainstorm.** If you installed it before the rename: with the plugin, run `claude plugin uninstall brainstorm@xiaolang-skills` and install `think-brainstorm@xiaolang-skills`; with `install.sh`, after `git pull` remove the old links (`rm ~/.agents/skills/brainstorm ~/.claude/skills/brainstorm`, only if they point into this repository) and run `./install.sh` again.

## License

MIT
