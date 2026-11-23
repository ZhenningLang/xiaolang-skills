# Figures of the brainstorm README

The pictures in [skills/think-brainstorm/README.md](../../../skills/think-brainstorm/README.md) and its Chinese page. Each one has an English and a Chinese file.

| Figure | What it shows | Definition in `src/` |
|:---|:---|:---|
| `run-flow` | The stages of one run, the grouping, the rewrites and the report | `run-flow.<lang>.d2`, written by hand |
| `three-comparisons` | The three comparisons the README speaks of | `three-comparisons.<lang>.d2`, written by hand |
| `human-score` | The score of each repetition in the comparison scored by a person | `human-score.<lang>.vl.json` |
| `six-questions` | Six real-world questions: plain asking, and the skill-only mechanisms left after the judge | `six-questions.<lang>.vl.json` |
| `skill-gain` | At equal idea calls: what plain asking found, and what the skill reached beyond it | `skill-gain.<lang>.vl.json` |
| `plain-curve` | How plain asking adds mechanisms call after call | `plain-curve.<lang>.vl.json` |
| `grouper-steady` | Skill-only mechanisms when the same ideas are grouped again | `grouper-steady.<lang>.vl.json` |
| `methods-compare` | The four default methods against the library of 18 | `methods-compare.<lang>.vl.json` |

## Where the numbers come from

`build_specs.py` reads the files under `evals/brainstorm/results` and writes every chart definition (`*.vl.json`, Vega-Lite). No number of a chart is typed by hand. A test (`evals/brainstorm/tests/test_docs.py`) fails when a definition in `src/` differs from what the script writes now.

The two diagrams hold these numbers by hand: the calls of each stage, 30 and about 31 calls, 46 to 62, 17, and 10, 10 against 4, 3. Change them when the README changes.

## To change a figure

1. Charts: change the result file or `build_specs.py`, then run `python3 docs/figures/brainstorm/build_specs.py`. Diagrams: edit the `.d2` file.
2. Render the definition to the PNG of the same name in this folder. The charts are plain Vega-Lite; the diagrams are D2.
3. The PNG files here were rendered with the author's own style layer, which is not in this repository. The class names in the `.d2` files (`box`, `lane`, `store`, `new`, `changed`, `hero`) belong to that layer. Another renderer gives the same content with a different look.

The first line of each definition (`fig:`) records the style that was chosen. A renderer can ignore it.
