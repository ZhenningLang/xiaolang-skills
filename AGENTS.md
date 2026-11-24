# Rules for agents working in this repo

This is a public collection of agent skills. Anyone can read every file.

- Each skill lives in `skills/<name>/SKILL.md`. A skill that helps you think is named `think-<x>`; its folders under `evals/` and `docs/figures/` use `<x>` alone, since `evals/<x>` is a Python package. Frontmatter uses only fields of the open Agent Skills specification: `name` and `description` (every supported host needs both), `license: MIT`, and `metadata` with `version` as a quoted string such as `"1.0"`. Raise `version` when the skill's behaviour changes, and keep it in step with the `v<N>` in the READMEs. Do not add host-only fields (for example Claude Code's `disable-model-invocation`): uploads to claude.ai and the specification's validator reject unknown fields. Do not put a `version` in `.claude-plugin/marketplace.json`; without one, Claude Code treats every commit as an update.
- Something an agent never needs to decide to use is a tool, not a skill. A tool lives in `tools/<name>/`: its command-line program is `scripts/<name with - as _>.py` (executable, standard library only), which `install.sh` links into `~/.local/bin` so that `git <x>` or `<name>` runs it; an optional slash command the user runs by hand is `commands/<name>.md`, shipped through the Claude Code plugin. A tool has the same READMEs, tests and evals as a skill, and no `SKILL.md`.
- A skill must work on its own. Never reference skills, tools, paths or config that only exist on the author's machine.
- Name the host-specific tool only when unavoidable. Say "spawn N sub-agents in one reply" instead of naming `Agent`, `task` or `subagent`.
- When a skill uses sub-agents, forbid fork-style sub-agents that inherit the parent conversation.
- Register every new skill or tool in `.claude-plugin/marketplace.json` and in the skill or tool table of both root READMEs.
- Each skill and tool has `README.md` and `README.zh.md` in its own folder, written for people: what it does, when to use it, evidence and limits. The root READMEs hold only the skill and tool tables, the map of the repository and the install steps. Keep `README.md` and `README.zh.md` saying the same thing. `SKILL.md` is for the agent; keep the two apart.
- Before every push run `scripts/check-leaks.sh` and `claude plugin validate .`; both must pass.
- Install logic lives in `install.sh`. Test it with a temporary `HOME`, never the real one.
- A number in a README must be in a file under `evals/<skill>/results/`. When a later measurement replaces a number, change the README to the new number. Do not keep the old number in the README: the result files and the git history hold it.
- A README for people has figures, not only text and tables. Figures of a skill live in `docs/figures/<skill>/` with their definitions in `src/`; a chart takes its numbers from the result files through a script, never typed by hand. Each figure has an English and a Chinese file.
- `docs/history/` is frozen. Do not update it and do not cite it as the current design.
