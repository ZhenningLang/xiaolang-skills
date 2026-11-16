"""Write the chart definitions (Vega-Lite) of the brainstorm README from the result files.

Every number in a chart is read from evals/brainstorm/results. Run this after a result file changes,
then render the definitions in src/ again. The flow diagrams (src/*.d2) are written by hand.

    python3 docs/figures/brainstorm/build_specs.py
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RESULTS = ROOT / "evals" / "brainstorm" / "results"
SRC = HERE / "src"

TEXT = {
    "en": {
        "cli": "CLI tool onboarding", "income": "First income of a newsletter",
        "curve_title": "Plain asking: new mechanisms come slower and slower",
        "curve_x": "Idea calls so far", "curve_y": "Different mechanisms so far", "question": "Question",
        "score_title": "Mechanisms the author had not thought of and cares about, of 17 sampled",
        "direct": "Asking the model directly", "full": "Full flow", "rep": "Repetition", "score_y": "Score (of 17)",
        "side": "",
        "steady_title": "Skill-only mechanisms: the same ideas, grouped again",
        "sonnet": "Sonnet grouper, 3 times", "opus": "Opus grouper, 2 times", "count": "Skill-only mechanisms",
        "methods_title": "4 methods and the library of 18: the ranges overlap",
        "default": "4 methods (default)", "library": "Library of 18",
        "by_grouper": "by the grouper, 2 groupings", "after_judge": "left after the judge, 4 passes",
        "methods": "Methods", "why": "counts read from the result files",
        "gain_title": "About 30 idea calls on each side: what plain asking found, and what the skill found beyond it",
        "gain_plain": "Plain asking: mechanisms found", "gain_grouper": "Skill: mechanisms plain asking did not reach, by the grouper",
        "gain_judge": "Skill: the same, after the judge removed repeats", "gain_x": "Different mechanisms",
        "cli_rough": "CLI tool onboarding (earlier method, a rough estimate after the judge)",
    },
    "zh": {
        "cli": "命令行工具上手", "income": "公众号第一笔收入",
        "curve_title": "反复直接问：新机制越来越少",
        "curve_x": "累计出点子调用次数", "curve_y": "累计不同机制数", "question": "题目",
        "score_title": "抽样的 17 个机制里，作者「没想到且在乎」的个数",
        "direct": "直接调用模型", "full": "完整流程", "rep": "重复", "score_y": "分数（满分 17）",
        "side": "",
        "steady_title": "技能独有机制：同一批点子，重新分组",
        "sonnet": "Sonnet 分组，3 次", "opus": "Opus 分组，2 次", "count": "技能独有机制数",
        "methods_title": "4 条方法和 18 条方法库：范围重叠",
        "default": "4 条方法（默认）", "library": "18 条方法库",
        "by_grouper": "分组者口径，2 次分组", "after_judge": "评委去重后剩下，4 遍",
        "methods": "方法", "why": "计数取自结果文件",
        "gain_title": "两边各约 30 次出点子调用：反复直接问找到的，和技能另外多找到的",
        "gain_plain": "反复直接问：找到的机制", "gain_grouper": "技能：反复直接问没想到的机制，分组者口径",
        "gain_judge": "技能：同上，评委去掉重复后", "gain_x": "不同机制的个数",
        "cli_rough": "命令行工具上手（早期量法，评委去重后是粗略估计）",
    },
}


def result(name):
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def spec(language, body):
    meta = {"content": "data", "style": "cool-clean", "candidates": ["cool-clean"],
            "why": TEXT[language]["why"], "surface": "repo-doc"}
    return {"usermeta": {"fig": meta}, **body}


def curve(language):
    t = TEXT[language]
    sides = (("cli", result("2026-11-12-equal-calls-cli.json")),
             ("income", result("2026-11-13-equal-calls-side-income.json")["compare"]))
    values = [{"calls": row["calls_so_far"], "mechanisms": row["mechanisms_so_far"], "question": t[key]}
              for key, side in sides for row in side["control"]["curve"]]
    encoding = {
        "x": {"field": "calls", "type": "quantitative", "title": t["curve_x"],
              "scale": {"domain": [0, 30]}, "axis": {"values": list(range(0, 31, 3))}},
        "y": {"field": "mechanisms", "type": "quantitative", "title": t["curve_y"], "scale": {"domain": [0, 30]}},
        "color": {"field": "question", "type": "nominal", "title": t["question"], "legend": {"orient": "bottom"}},
    }
    return spec(language, {
        "title": t["curve_title"], "width": 560, "height": 300, "data": {"values": values},
        "layer": [
            {"mark": {"type": "line", "point": True}, "encoding": encoding},
            {"mark": {"type": "text", "dy": -12, "fontSize": 14}, "encoding": {**encoding, "text": {"field": "mechanisms"}}},
        ],
    })


def score(language):
    t = TEXT[language]
    units = result("2026-11-08-human-scored-cli.json")["score"]["units"]
    values = [{"side": t[side], "rep": f"{t['rep']} {number}", "score": units[f"{side}/rep-{number}"]}
              for side in ("direct", "full") for number in (1, 2)]
    encoding = {
        "x": {"field": "side", "type": "nominal", "title": None, "sort": [t["direct"], t["full"]],
              "axis": {"labelAngle": 0, "labelFontSize": 14}},
        "xOffset": {"field": "rep"},
        "y": {"field": "score", "type": "quantitative", "title": t["score_y"], "scale": {"domain": [0, 17]}},
    }
    return spec(language, {
        "title": t["score_title"], "width": 480, "height": 300, "data": {"values": values},
        "layer": [
            {"mark": "bar", "encoding": {**encoding, "color": {"field": "rep", "type": "nominal", "title": None,
                                                              "legend": {"orient": "bottom"}}}},
            {"mark": {"type": "text", "dy": -8, "fontSize": 15, "fontWeight": "bold"},
             "encoding": {**encoding, "text": {"field": "score"}}},
        ],
    })


def strip(language, title, rows, color_title=None, domain=(20, 120), x_title=None):
    """One row of points per label; every point carries its number."""
    t = TEXT[language]
    order = list(dict.fromkeys(row["row"] for row in rows))
    for label in order:
        for rank, row in enumerate(sorted((row for row in rows if row["row"] == label), key=lambda row: row["count"])):
            row["below"] = rank % 2
    encoding = {
        "y": {"field": "row", "type": "nominal", "title": None, "sort": order,
              "axis": {"labelFontSize": 14, "labelLimit": 520}},
        "x": {"field": "count", "type": "quantitative", "title": x_title or t["count"], "scale": {"domain": list(domain)}},
    }
    point = dict(encoding)
    if color_title:
        point["color"] = {"field": "group", "type": "nominal", "title": color_title,
                          "legend": {"orient": "bottom", "direction": "vertical", "labelLimit": 700}}
    return spec(language, {
        "title": title, "width": 460, "height": 56 * len(order) + 40, "data": {"values": rows},
        "layer": [
            {"mark": {"type": "point", "filled": True, "size": 160, "opacity": 0.85}, "encoding": point},
            # Neighbours in one row take turns above and below, so close numbers do not cover each other.
            {"transform": [{"filter": "datum.below == 0"}], "mark": {"type": "text", "dy": -16, "fontSize": 14},
             "encoding": {**encoding, "text": {"field": "count"}}},
            {"transform": [{"filter": "datum.below == 1"}], "mark": {"type": "text", "dy": 18, "fontSize": 14},
             "encoding": {**encoding, "text": {"field": "count"}}},
        ],
    })


def steady(language):
    t = TEXT[language]
    data = result("2026-11-14-grouper-repeat-side-income.json")
    rows = [{"row": t[key], "count": run["skill_only"]}
            for key, name in (("sonnet", "sonnet_groupings"), ("opus", "opus_groupings")) for run in data[name]["runs"]]
    return strip(language, t["steady_title"], rows)


def methods(language):
    t = TEXT[language]
    data = result("2026-11-14-methods-default-vs-library-side-income.json")
    audit = data["audit"]["by_grouping"]
    grouped = {"default": data["default_opus_groupings"]["skill_only"],
               "library": [run["skill_only"] for run in data["library_opus_groupings"]["runs"]]}
    rows = []
    for key in ("default", "library"):
        left = [audit[f"{key}-opus-{number}"][f"left_pass{turn}"] for number in (1, 2) for turn in (1, 2)]
        for kind, counts in (("by_grouper", grouped[key]), ("after_judge", left)):
            rows += [{"row": f"{t[key]} · {t[kind]}", "group": t[key], "count": count} for count in counts]
    return strip(language, t["methods_title"], rows, color_title=t["methods"])


def gain(language):
    """Both sides at equal idea calls: plain asking, and what the skill reached beyond it."""
    t = TEXT[language]
    first = result("2026-11-12-equal-calls-cli.json")
    second = result("2026-11-13-equal-calls-side-income.json")["compare"]
    current = result("2026-11-14-methods-default-vs-library-side-income.json")
    audit = current["audit"]["by_grouping"]
    left = [audit[f"default-opus-{number}"][f"left_pass{turn}"] for number in (1, 2) for turn in (1, 2)]
    counts = {
        t["cli_rough"]: ([first["control"]["mechanisms"]], [first["same_grouper"]["skill_only"]],
                         [first["audit_fixed_rule"]["repeats"]["estimated_new"]]),
        t["income"]: ([second["control"]["mechanisms"]], current["default_opus_groupings"]["skill_only"], left),
    }
    rows = [{"row": t[label], "group": question, "count": count}
            for question, kinds in counts.items()
            for label, values in zip(("gain_plain", "gain_grouper", "gain_judge"), kinds) for count in values]
    return strip(language, t["gain_title"], rows, color_title=t["question"], domain=(0, 120), x_title=t["gain_x"])


CHARTS = {"skill-gain": gain, "plain-curve": curve, "human-score": score, "grouper-steady": steady, "methods-compare": methods}


def build():
    """Chart definitions by file name."""
    return {f"{name}.{language}.vl.json": make(language) for name, make in CHARTS.items() for language in TEXT}


if __name__ == "__main__":
    SRC.mkdir(exist_ok=True)
    for name, body in build().items():
        (SRC / name).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("wrote", name)
