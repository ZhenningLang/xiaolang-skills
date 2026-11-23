# Figures of the git-tidy README

The pictures in [skills/git-tidy/README.md](../../../skills/git-tidy/README.md) and its Chinese page. Each one has an English and a Chinese file.

| Figure | What it shows | Definition in `src/` |
|:---|:---|:---|
| `flow` | plan, the decision, apply and the restore record | `flow.<lang>.d2`, written by hand |
| `scenarios` | The twelve scenarios: right, deletes what should stay, or keeps what could go, for git-tidy and for `git branch --merged` | `scenarios.<lang>.vl.json` |

## Where the numbers come from

`build_specs.py` reads the latest `evals/git-tidy/results/*-scenarios.json` and writes the chart definitions (`*.vl.json`, Vega-Lite). No cell of the chart is typed by hand. The flow diagram holds no measured numbers.

## To change a figure

1. Chart: run the evaluation again or change `build_specs.py`, then run `python3 docs/figures/git-tidy/build_specs.py`. Diagram: edit the `.d2` file.
2. Render the definition to the PNG of the same name in this folder. The chart is plain Vega-Lite; the diagram is D2.
3. The PNG files here were rendered with the author's own style layer, which is not in this repository. The class names in the `.d2` files (`box`, `store`, `decision`, `new`, `hero`) belong to that layer. Another renderer gives the same content with a different look.
