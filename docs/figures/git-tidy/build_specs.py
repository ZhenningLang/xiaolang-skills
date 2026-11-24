"""Write the chart definitions (Vega-Lite) of the git-tidy README from the result files.

Every cell of the chart is read from the latest evals/git-tidy/results/*-scenarios.json.
Run this after a result file changes, then render the definitions in src/ again.
The flow diagram (src/flow.<lang>.d2) is written by hand.

    python3 docs/figures/git-tidy/build_specs.py
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RESULTS = ROOT / "evals" / "git-tidy" / "results"
SRC = HERE / "src"

TEXT = {
    "en": {
        "title": "12 cleanup scenarios: what each way of choosing would delete",
        "tool_tidy": "git-tidy", "tool_usual": "git branch --merged",
        "right": "right", "wrong_delete": "deletes what should stay", "missed": "keeps what could go",
        "result": "Result", "scenario": "Scenario", "tool": "",
        "scenarios": {
            "merge-commit": "Merge commit", "fast-forward": "Fast-forward", "squash-merge": "Squash merge",
            "rebase-merge": "Rebase merge", "unmerged": "Not merged", "merged-then-more": "Merged, then new commit",
            "worktree-uncommitted": "Worktree: uncommitted edit", "worktree-untracked": "Worktree: untracked file",
            "worktree-ignored-env": "Worktree: only copy of .env", "protected-branch": "Shared branch (develop)",
            "remote-open-pr": "Remote branch with open PR", "local-main-only": "Merged into unpushed local main",
        },
        "group_delete": "should delete", "group_keep": "should keep",
    },
    "zh": {
        "title": "12 个清理场景：两种挑法各会删掉什么",
        "tool_tidy": "git-tidy", "tool_usual": "git branch --merged",
        "right": "判对", "wrong_delete": "删了该留的", "missed": "留了能删的",
        "result": "结果", "scenario": "场景", "tool": "",
        "scenarios": {
            "merge-commit": "合并提交", "fast-forward": "快进合并", "squash-merge": "压缩合并",
            "rebase-merge": "变基合并", "unmerged": "没合并", "merged-then-more": "合并后又加了提交",
            "worktree-uncommitted": "worktree：有未提交改动", "worktree-untracked": "worktree：有未跟踪文件",
            "worktree-ignored-env": "worktree：唯一一份 .env", "protected-branch": "共享分支（develop）",
            "remote-open-pr": "远端分支有开着的 PR", "local-main-only": "只合进了没推送的本地 main",
        },
        "group_delete": "应删", "group_keep": "应留",
    },
}


def latest():
    files = sorted(RESULTS.glob("*-scenarios.json"))
    return json.loads(files[-1].read_text()), files[-1].name


def outcome(truth, deletes):
    if truth == "delete":
        return "right" if deletes else "missed"
    return "wrong_delete" if deletes else "right"


def chart(lang, data, source):
    t = TEXT[lang]
    rows = []
    for s in data["scenarios"]:
        label = f"{t['scenarios'][s['id']]} ({t['group_' + s['truth']]})"
        for key, tool in (("git_tidy", t["tool_tidy"]), ("one_liner", t["tool_usual"])):
            rows.append({"scenario": label, "tool": tool, "result": t[outcome(s["truth"], s[key]["deletes"])]})
    order = [f"{t['scenarios'][s['id']]} ({t['group_' + s['truth']]})" for s in data["scenarios"]]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "usermeta": {"fig": {"content": "data", "style": "cool-clean", "candidates": ["cool-clean"],
                             "why": "cells read from the result file", "surface": "repo-doc"},
                     "source": f"evals/git-tidy/results/{source}"},
        "title": t["title"],
        "data": {"values": rows},
        "width": 260, "height": {"step": 26},
        "mark": {"type": "rect", "stroke": "white", "strokeWidth": 2},
        "encoding": {
            "x": {"field": "tool", "type": "nominal", "title": t["tool"], "sort": [t["tool_tidy"], t["tool_usual"]],
                  "axis": {"orient": "top", "labelAngle": 0}},
            "y": {"field": "scenario", "type": "nominal", "title": None, "sort": order, "axis": {"labelLimit": 400}},
            "color": {"field": "result", "type": "nominal", "title": t["result"],
                      "scale": {"domain": [t["right"], t["wrong_delete"], t["missed"]],
                                "range": ["#9fd3b4", "#e8776b", "#f2c26b"]},
                      "legend": {"orient": "bottom", "labelLimit": 300}},
        },
    }


def main():
    data, source = latest()
    SRC.mkdir(exist_ok=True)
    for lang in TEXT:
        out = SRC / f"scenarios.{lang}.vl.json"
        out.write_text(json.dumps(chart(lang, data, source), ensure_ascii=False, indent=2) + "\n")
        print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
