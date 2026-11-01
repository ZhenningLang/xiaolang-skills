# 小郎的技能库

[English](README.md)

我自己每天在用、觉得值得分享的 agent 技能。作者在公众号和 X 上叫「小郎碎碎念」。

## 技能

| 技能 | 做什么 | 状态 |
|:---|:---|:---|
| [brainstorm](skills/brainstorm/SKILL.md) | 头脑风暴：先把点子撑开，再谈好坏 | 设计中，暂不可用 |

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
