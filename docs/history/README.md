# History

Design notes written before v1 of the brainstorm skill was released (November 2026). They are in Chinese and are kept as a record only. **They do not describe the skill as shipped.** For the current state read [skills/brainstorm/README.md](../../skills/brainstorm/README.md).

## What the notes planned and what shipped

| The notes say | What shipped |
|:---|:---|
| A flow of eight steps: choose angles, diverge, pressure, combine, a new judge ranks the ideas, a trial plan | Baseline, two rounds, three pressure levels, grouping by mechanism. No combining, no ranking, no trial plan |
| Four pressure levels | Three |
| Agents on several models, agents free to search | One model family, no search |
| Five development questions and three held-out questions | Three questions written; one scored by a person, two counted against plain asking |
| Every step gets a keep-or-delete verdict before release | No step has its own verdict |
| "State: approved" or "draft, waiting for review" in the file headers | Neither is current; the files are frozen |

## Words that mean something else here

- **机制 (mechanism)** in these notes means a step of the flow (blind rounds, the three-step prompt, pressure). In the skill and its README, a mechanism is what makes an idea work.
- **评委 (judge)** in these notes ranks ideas. The shipped skill never ranks; the judge in the evaluation only finds repeats and constraint violations.

## Files

| File | What it was |
|:---|:---|
| `overview.md` | The decisions the author had to make, as registered items |
| `approval.md` | The author's rulings on those decisions |
| `mechanisms.md` | The steps of the planned flow, each as a hypothesis to test |
| `evaluation.md` | The planned exam set and scoring |
| `brainstorm-flow.png`, `brainstorm-flow.scene.js` | Diagram of the planned eight-step flow, and its source |
| `evaluation-loop.png`, `evaluation-loop.d2` | Diagram of the planned evaluation loop, and its source |
| `approval.comments.json`, `approval.review-points.json` | Records of the author's review of `approval.md`, written by a review tool that is not in this repository |
