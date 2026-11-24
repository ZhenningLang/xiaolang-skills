# Figures of the think-compare README

The pictures in [skills/think-compare/README.md](../../../skills/think-compare/README.md) and its Chinese page. Each one has an English and a Chinese file.

| Figure | What it shows | Definition in `src/` |
|:---|:---|:---|
| `method` | Candidates, dimensions first, one row per dimension, the verdict | `method.<lang>.d2`, written by hand |
| `measure` | How the evaluation was run: two arms, a judge from another company, both orders | `measure.<lang>.d2`, written by hand |
| `preferences` | Which answer the judge preferred, per question and order | `preferences.<lang>.vl.json` |
| `criteria` | Mean score per criterion for each arm | `criteria.<lang>.vl.json` |

## Where the numbers come from

`build_specs.py` reads the latest `evals/compare/results/*-eight-questions.json` and writes the chart definitions (`*.vl.json`, Vega-Lite). No number of a chart is typed by hand. The two diagrams hold no measured numbers.

## To change a figure

1. Charts: run the evaluation again or change `build_specs.py`, then run `python3 docs/figures/compare/build_specs.py`. Diagrams: edit the `.d2` file.
2. Render the definition to the PNG of the same name in this folder. The charts are plain Vega-Lite; the diagrams are D2.
3. The PNG files here were rendered with the author's own style layer, which is not in this repository. The class names in the `.d2` files belong to that layer. Another renderer gives the same content with a different look.
