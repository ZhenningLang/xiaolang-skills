"""Write the chart definitions (Vega-Lite) of the think-compare README from the result file.

Every number and cell of the charts is read from the latest evals/compare/results/*-eight-questions.json.
Run this after a result file changes, then render the definitions in src/ again.
The two diagrams (src/method.<lang>.d2, src/measure.<lang>.d2) hold no measured numbers and are written by hand.

    python3 docs/figures/compare/build_specs.py
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RESULTS = ROOT / "evals" / "compare" / "results"
SRC = HERE / "src"

TEXT = {
    "en": {
        "pref_title": "Which answer the judge preferred, per question and order",
        "order": {"first": "first order", "second": "labels swapped"},
        "arm": {"skill": "with the skill", "plain": "plain asking", "tie": "tie"},
        "preferred": "Preferred",
        "crit_title": "Mean score per criterion (0 to 2), {n} judgments",
        "criteria": {"C1": "C1 criteria first", "C2": "C2 concrete", "C3": "C3 uses the situation",
                     "C4": "C4 clear verdict", "C5": "C5 conditions", "C6": "C6 accuracy and honesty"},
        "score": "Mean score", "answer": "Answer",
    },
    "zh": {
        "pref_title": "评委每道题、每种顺序偏好哪份回答",
        "order": {"first": "第一种顺序", "second": "标签对调"},
        "arm": {"skill": "用技能", "plain": "直接问", "tie": "平手"},
        "preferred": "偏好",
        "crit_title": "每条标准的平均分（0 到 2），共 {n} 次评判",
        "criteria": {"C1": "C1 先定维度", "C2": "C2 比得具体", "C3": "C3 用上题目条件",
                     "C4": "C4 裁决明确", "C5": "C5 写明前提", "C6": "C6 准确与诚实"},
        "score": "平均分", "answer": "回答",
    },
}
COLORS = {"skill": "#5b8def", "plain": "#f2a65a", "tie": "#c9c9c9"}
CRITERIA = ("C1", "C2", "C3", "C4", "C5", "C6")


def latest():
    files = sorted(RESULTS.glob("*-eight-questions.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")), files[-1].name


def usermeta(source):
    return {"fig": {"content": "data", "style": "cool-clean", "candidates": ["cool-clean"],
                    "why": "cells and bars read from the result file", "surface": "repo-doc"},
            "source": f"evals/compare/results/{source}"}


def preferences(lang, data, source):
    t = TEXT[lang]
    rows = []
    for q in data["questions"]:
        result = data["per_question"][q["id"]]
        for row in result["orders"]:
            rows.append({"question": q["id"], "order": t["order"][row["order"]],
                         "preferred": t["arm"][row["preferred"]]})
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "usermeta": usermeta(source),
        "title": t["pref_title"],
        "data": {"values": rows},
        "width": 240, "height": {"step": 28},
        "mark": {"type": "rect", "stroke": "white", "strokeWidth": 2},
        "encoding": {
            "x": {"field": "order", "type": "nominal", "title": None,
                  "sort": [t["order"]["first"], t["order"]["second"]], "axis": {"orient": "top", "labelAngle": 0}},
            "y": {"field": "question", "type": "nominal", "title": None,
                  "sort": [q["id"] for q in data["questions"]]},
            "color": {"field": "preferred", "type": "nominal", "title": t["preferred"],
                      "scale": {"domain": [t["arm"][a] for a in COLORS], "range": list(COLORS.values())},
                      "legend": {"orient": "bottom"}},
        },
    }


def criteria(lang, data, source):
    t = TEXT[lang]
    rows = [{"criterion": t["criteria"][c], "answer": t["arm"][arm], "score": data["mean_scores"][arm][c]}
            for c in CRITERIA for arm in ("skill", "plain")]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "usermeta": usermeta(source),
        "title": t["crit_title"].format(n=data["counts"]["judgments"]),
        "data": {"values": rows},
        "width": 320, "height": {"step": 14},
        "mark": {"type": "bar"},
        "encoding": {
            "y": {"field": "criterion", "type": "nominal", "title": None,
                  "sort": [t["criteria"][c] for c in CRITERIA], "axis": {"labelLimit": 300}},
            "yOffset": {"field": "answer", "sort": [t["arm"]["skill"], t["arm"]["plain"]]},
            "x": {"field": "score", "type": "quantitative", "title": t["score"], "scale": {"domain": [0, 2]},
                  "axis": {"values": [0, 0.5, 1, 1.5, 2]}},
            "color": {"field": "answer", "type": "nominal", "title": t["answer"],
                      "scale": {"domain": [t["arm"]["skill"], t["arm"]["plain"]],
                                "range": [COLORS["skill"], COLORS["plain"]]},
                      "legend": {"orient": "bottom"}},
        },
    }


def build():
    """Every chart definition by file name."""
    data, source = latest()
    return {f"{name}.{lang}.vl.json": make(lang, data, source)
            for lang in TEXT for name, make in (("preferences", preferences), ("criteria", criteria))}


def main():
    SRC.mkdir(exist_ok=True)
    for name, body in build().items():
        out = SRC / name
        out.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
