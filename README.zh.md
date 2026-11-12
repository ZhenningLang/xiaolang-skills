# 小郎的技能库

[English](README.md)

我自己每天在用、觉得值得分享的 agent 技能。作者在公众号和 X 上叫「小郎碎碎念」。

## 技能

| 技能 | 做什么 | 状态 |
|:---|:---|:---|
| [brainstorm](skills/brainstorm/SKILL.md) | 头脑风暴：先把点子撑开，再谈好坏 | v1，只评过一道题，见[证据与局限](#证据与局限) |

这些技能的评测工具见 [evals/README.md](evals/README.md)。它们只做测量，不是技能。
## 证据与局限

头脑风暴 v1 测到什么程度，如实写在这里：

- **只评了一道题**：个人维护的开源命令行工具，怎样降低新用户第一次上手就失败的比例。重复两次。
- **分数**：完整流程 10 分、10 分；直接问模型 4 分、3 分。每个单元抽 17 条，作者盲评。
- **分差从哪来**：分差全部来自三问里的「之前没想到」一问。
- **盲评不彻底**：从文字长度和方法腔能看出点子来自哪个流程。
- **没有查硬约束**：没有评委检查点子是否违反题目的硬约束。
- **评分那次对比不对等**：对照组（直接问）没给卡片；完整流程的成本约为直接问的 25 倍。
- **同样调用次数的对比，只跑了一次，没有人工评分**：后来用发布的技能在同一道题上跑了一次（31 次出点子调用，252 条），旁边是不给卡片、不给方法的反复直接问（30 次调用，300 条）。两边由同一个分组者归类：直接问归成 27 个机制，前 9 次调用之后几乎不再出新机制；技能归成 124 个，其中 101 个不在那 27 个里。另一个模型抽查了这 101 个里的 25 个，判定 2 个其实是重复。调用次数相同，token 不同：技能后几步的提示词长得多。这里数的是「不同的机制」，不是「有用的机制」。数据见 [evals/brainstorm/results](evals/brainstorm/results/)。
- **只有一种跑法有评分**：评分只在 Sonnet 直接调用、不带工具的方式下做过。本技能里由你的 coding agent 派发子 agent 的版本，只有上面的机制数，没有人工评分。
- **没有逐步证据**：方法、卡片、退回重写、多轮、各级加压，没有哪一步有单独的去留证据。

## 安装

**Claude Code**：

```bash
claude plugin marketplace add ZhenningLang/xiaolang-skills
claude plugin install brainstorm@xiaolang-skills
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

## 许可证

MIT
