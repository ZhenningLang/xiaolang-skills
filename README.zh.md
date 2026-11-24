# 小郎的技能库

[English](README.md)

我自己每天在用、觉得值得分享的 agent 技能。作者在公众号和 X 上叫「小郎碎碎念」。

## 技能

| 技能 | 做什么 | 状态 |
|:---|:---|:---|
| [think-brainstorm](skills/think-brainstorm/README.zh.md) | 头脑风暴：先把点子撑开，再谈好坏 | v1。人工评分只做过一道题。和反复直接问比过八道题的计数；在六道真实新题上多 67 到 118 个机制。前两道题里：它能想到反复直接问想不到的机制，在认真量过的那道题上多 46 到 62 个（中文方法原文；现在发布的英文方法原文跑过一次，多 33 到 39 个）。见[用了技能之后的提升](skills/think-brainstorm/README.zh.md#二用了技能之后的提升) |
| [git-tidy](skills/git-tidy/README.zh.md) | 清理做完的 worktree 和分支，只删不会丢东西的 | v1。在 12 个搭出来的场景里，没删一个该留的，也没留一个能删的；`git branch --merged` 会删掉 6 个该留的、漏掉 2 个能删的。见[证据](skills/git-tidy/README.zh.md#3-证据) |

每个技能在自己的目录里有一份 README：做什么、什么时候用、测到什么程度。

## 仓库里有什么

| 路径 | 是什么 |
|:---|:---|
| `skills/<名字>/` | 一个技能：给 agent 读的 `SKILL.md`，给人读的 `README.md`，它的脚本和测试 |
| `evals/` | 测量技能的工具和结果文件。它们不是技能。从 [evals/README.md](evals/README.md) 读起 |
| `docs/figures/` | 技能说明页的配图，以及渲染它们用的图定义 |
| `docs/history/` | v1 之前的设计稿，只为留痕。它们不代表发布的技能 |
| `install.sh`、`.claude-plugin/` | 两种安装方式 |
| `AGENTS.md` | 给修改本仓库的 agent 的规则 |

## 安装

**Claude Code**：

```bash
claude plugin marketplace add ZhenningLang/xiaolang-skills
claude plugin install think-brainstorm@xiaolang-skills
claude plugin install git-tidy@xiaolang-skills
```

**kilo、opencode、kimi code、Grok Bot，以及不想用插件的 Claude Code**：

```bash
git clone https://github.com/ZhenningLang/xiaolang-skills.git
cd xiaolang-skills
./install.sh                    # 全部技能，装到 ~/.agents/skills 和 ~/.claude/skills
./install.sh --target agents    # 只装到 ~/.agents/skills
./install.sh --uninstall        # 卸载
```

`install.sh` 默认用软链，`git pull` 之后自动生效。已经存在、且不是它装的同名目录，它不会碰。

**brainstorm 已改名为 think-brainstorm。** 改名前装过的：用插件装的，先 `claude plugin uninstall brainstorm@xiaolang-skills`，再装 `think-brainstorm@xiaolang-skills`；用 `install.sh` 装的，`git pull` 后删掉旧链接（`rm ~/.agents/skills/brainstorm ~/.claude/skills/brainstorm`，只在它们指向本仓库时删），再跑一次 `./install.sh`。

## 许可证

MIT
