# Evaluation of the think-compare skill

Does the skill help Claude answer a "which one should we pick" question? One tool, `run.py`, answers eight common technology-selection questions twice, once with the skill and once by plain asking, and has a judge from another company grade each pair blind. What the results mean is in [skills/think-compare/README.md](../../skills/think-compare/README.md#3-evidence).

Python 3.9+, the standard library, the Claude Code CLI (`claude`) for the answers, and any command that can reach a judge model.

## What is here

| File | What it is |
|:---|:---|
| `questions.json` | The eight questions. Written for this evaluation; none comes from a real project |
| `rubric.md` | The judge's instructions: six criteria scored 0 to 2, then a preference. Sent word for word |
| `run.py` | `answer`, `judge` and `summarize` |
| `results/` | One file per measurement, with every answer and every judge reply in it |
| `tests/` | Tests with a fake `claude` and a fake judge; no model is called |

## The steps

```bash
RUN=/tmp/compare-run   # any directory outside the repository

python3 -m evals.compare.run answer --run-dir $RUN --arm skill
python3 -m evals.compare.run answer --run-dir $RUN --arm plain
python3 -m evals.compare.run judge  --run-dir $RUN --judge-cmd "<command>" --judge-label "<judge model>"
python3 -m evals.compare.run summarize --run-dir $RUN --label "<what this run is>" \
    --out evals/compare/results/<date>-eight-questions.json
```

Each step keeps the files it has finished and skips them on a rerun, so a failed call costs only itself. `--questions database,cache` limits any step to some questions.

- **`answer`** runs `claude -p` from an empty temporary directory with no tools, no settings files, no MCP servers and no skills or slash commands, so neither arm sees the user's own setup. The skill arm adds `SKILL.md` to the system prompt with one line saying the request matches it; the plain arm sends the question alone. Default model `claude-opus-5-5` (`--model`).
- **`judge`** sends each pair twice: once with the skill answer under one label, once with the labels swapped. Which label comes first is drawn from `--seed` per question. The prompt holds the rubric, the question and the two answers under the labels A and B, nothing else. `--judge-cmd` is any command that reads the prompt on stdin and prints the reply on stdout. A reply that is not the JSON the rubric asks for is retried once (`--attempts`), then recorded as failed.
- **`summarize`** maps the labels back to the two arms and writes one result file: counts, mean scores, a summary per question, and the questions, the rubric, every answer and every judge reply.

Use a judge from another company than the one whose model answered. A judge that runs inside a coding agent can carry that agent's own instructions (a global rules file, plugins): run it with those switched off, and check by asking it what language it was told to reply in.

## What the counts mean

| Field | Meaning |
|:---|:---|
| `preferred_skill`, `preferred_plain`, `tie` | Over all judgments (two per question): which answer the judge preferred |
| `preferred_label_A`, `preferred_label_B` | The same, by label. Far from even means the judge favours a position |
| `questions_skill_both_orders`, `questions_plain_both_orders`, `questions_split_or_tie` | Per question: the judge preferred the same answer in both orders, or did not |
| `questions_separated` | Per question: the same answer was preferred in both orders and also scored higher in both. The test of whether the rubric tells the two answers apart |
| `mean_scores` | Mean of each criterion and of the total (0 to 12) per arm, over all judgments |

## Result files

| File | What it is |
|:---|:---|
| `results/2026-11-24-spike.json` | The first two questions only, run before the rest to check that the rubric can tell the two answers apart |
| `results/2026-11-24-eight-questions.json` | All eight questions. The current result, used by the skill README and its charts. Its first two questions are the spike's answers and judgments |

The spike was judged twice. The first judging ran the judge with the global instructions of the agent it ran in, which ask for replies in another language and for labels on uncertain claims; criterion C6 rewards such labels, so those four judgments were set aside and the pairs judged again with the instructions switched off. Only the second judging is in the result files.

## Tests

```bash
python3 -m unittest discover -s evals/compare/tests -t .
```
