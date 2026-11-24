# Evaluation of the git-tidy tool

One tool: `run_scenarios.py`. It builds twelve throwaway repositories, each in a state whose right answer is known from how it was built, and asks two ways of choosing what to delete about each one: git-tidy's `plan`, and the selection step of the usual `git branch --merged` cleanup. It calls no model and needs only Python 3.9+ and git 2.38+.

```bash
python3 evals/git-tidy/run_scenarios.py
```

It writes `results/<date>-scenarios.json` and exits non-zero when git-tidy would delete anything that should stay. The scenarios, the right answers and both verdicts for each are in the result file; what they mean is in [tools/git-tidy/README.md](../../tools/git-tidy/README.md#3-evidence).

| Result file | What it is |
|:---|:---|
| `results/2026-11-23-scenarios.json` | The current result, used by the skill README and its chart |
