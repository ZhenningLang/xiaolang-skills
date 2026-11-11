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
- **对比不对等**：对照组（直接问）没给卡片；完整流程的成本约为直接问的 25 倍，没做同预算对比。
- **只测过一种跑法**：只在 Sonnet 直接调用、不带工具的方式下测过。本技能里由你的 coding agent 派发子 agent 的版本，没有评分。
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
