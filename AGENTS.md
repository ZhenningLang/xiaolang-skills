# Rules for agents working in this repo

This is a public collection of agent skills. Anyone can read every file.

- Each skill lives in `skills/<name>/SKILL.md`. Frontmatter has exactly `name` and `description`; every supported host needs both.
- A skill must work on its own. Never reference skills, tools, paths or config that only exist on the author's machine.
- Name the host-specific tool only when unavoidable. Say "spawn N sub-agents in one reply" instead of naming `Agent`, `task` or `subagent`.
- When a skill uses sub-agents, forbid fork-style sub-agents that inherit the parent conversation.
- Register every new skill in `.claude-plugin/marketplace.json` and in the skill table of both root READMEs.
- Each skill has `README.md` and `README.zh.md` in its own folder, written for people: what it does, when to use it, evidence and limits. The root READMEs hold only the skill table and the install steps. `SKILL.md` is for the agent; keep the two apart.
- Before every push run `scripts/check-leaks.sh` and `claude plugin validate .`; both must pass.
- Install logic lives in `install.sh`. Test it with a temporary `HOME`, never the real one.
