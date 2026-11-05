"""Acceptance decision, diagnostics and the human-readable report. Only `--judge-config` can spend money."""

from collections import Counter
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
import html
import json
from pathlib import Path
import re
import statistics

from .backends import NotStarted, amount, exact_sum
from .budget import BudgetLedger, atomic_write, cli_boundary, default_ledger_path
from .config import DEFAULTS, LEVELS, load_config, validate_config
from .exam import load_exams, safe_segment
from . import replacements
from .judge import evaluate, fake_judge, stage_backend
from .metrics import compare_units, compute_unit, dumps as metric_dumps, load_unit, noise_line, save_metrics
from .record import output_dir, read_file, read_rows
from .replace_unit import describe as describe_replacement
from .sheet import (KEY_NAME, MASK, MIN_UNIT_SAMPLE, check_conservation, check_issued, check_results, discover_units,
                    load_ideas, load_issued, load_table, load_verdicts, scored, unit_name)


DEFAULT_COST_RATIO = "1.5"
DEFAULT_CHECK_BAD_RATIO = "0.34"
DEFAULT_CONTRIBUTION_FLOOR = "0"
ROWS = {1: "evidence", 2: "injection", 3: "guardrail", 4: "better", 5: "worse", 6: "simpler"}
UNRECORDED_SWITCHES = {name: DEFAULTS[name] for name in ("three_step", "predict_peers", "serial_visible",
                                                         "before_generation", "decomposition_timing")}
AUTOMATIC_SYMPTOMS = {"unused_cards": ("点子不用分到的卡片", "材料分配或提示词"),
                      "return_same_mechanism": ("退回后机制没变", "去重判定或退回提示"),
                      "level_no_new": ("某级没有新机制", "压力升级或级别设置"),
                      "subproblem_empty": ("某子问题没有点子", "拆法汇总或指派")}
MANUAL_SYMPTOMS = {"method_same_mechanism": ("换方法后机制仍然相同", "方法写得不够具体"),
                   "dedupe_false_merge": ("去重把不同机制并在一起", "去重"),
                   "odd_after_exhaustion": ("枯竭后全是怪点子", "压力升级缺少约束"),
                   "judge_author_gap": ("评委高分的点子，作者都不计分", "评委")}
SYMPTOMS = {**AUTOMATIC_SYMPTOMS, **MANUAL_SYMPTOMS}
CONTRIBUTION_NOTE = "贡献度只说明相关，不说明因果：它只数了计分的机制是否都经过某环节，没有真的去掉这个环节。"
SAMPLE_NOTE = "样本小，只作方向判断。"
DETAIL_TITLE = "明细（需要查原因时再看）"
NOT_TESTED = "还没测"
KEEP, TRY_REMOVING, REMOVABLE, NO_DATA = "留下", "试着去掉", "可以删", "没数据"
LEVEL_WORDS = {"round1": "第一轮", "round2": "第二轮", "pressure1": "压力一级", "pressure2": "压力二级",
               "pressure3": "压力三级", "pressure4": "压力四级"}
SWITCH_WORDS = {"three_step": "三步法", "predict_peers": "预判同伴", "serial_visible": "串行可见",
                "before_generation": "禁区放在生成前", "decomposition_timing": "拆解时机"}
STAGE_WORDS = {"recording": "录制", "grouping": "去重分组", "constraint": "硬约束判定"}


def exact(value):
    return Fraction(str(value))


def as_decimal(value):
    return None if value is None else Decimal(value.numerator) / Decimal(value.denominator)


def mean(values):
    return statistics.mean(map(exact, values))


def json_ready(value):
    """Repetition numbers key the in-memory tables; JSON object keys must be strings."""
    if isinstance(value, dict):
        return {str(key): json_ready(entry) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(entry) for entry in value]
    return value


def known_positive(guard):
    return isinstance(guard, dict) and type(guard.get("violations")) is int and guard["violations"] > 0


def guard_unknown(guard):
    """Unknown is never zero. A known positive count is evidence of a violation, though only a lower bound."""
    if not isinstance(guard, dict) or guard.get("status") not in ("checked", "not_applicable", "not_evaluated"):
        return True
    if guard["status"] == "not_applicable":
        return False
    return not known_positive(guard) and (guard["status"] != "checked" or type(guard.get("violations")) is not int)


def constraint_unknown(guard):
    """A partial count is only a lower bound: it cannot show the guard held, so it is missing evidence."""
    return guard_unknown(guard) or guard["status"] == "not_evaluated"


def missing_evidence(row, configs):
    reasons = []
    scores, guards, costs = row.get("scores", {}), row.get("guards", {}), row.get("cost_usd", {})
    for config in configs:
        size = len(scores.get(config, []))
        entries = guards.get(config, [])
        if not entries or len(entries) != size or any(guard_unknown(entry.get("injection")) for entry in entries):
            reasons.append(f"{row['exam_id']}/{config} 缺护栏证据")
        elif any(constraint_unknown(entry.get("constraint")) for entry in entries):
            reasons.append(f"{row['exam_id']}/{config} 硬约束判定不完整，证明不了护栏没有恶化")
        spent = costs.get(config, [])
        if not spent or len(spent) != size or any(value is None or isinstance(value, bool) for value in spent):
            reasons.append(f"{row['exam_id']}/{config} 缺费用证据")
    return reasons


def known_violations(exams, field, *, incomplete_only=False):
    rows = []
    for row in exams:
        for config, entries in sorted(row.get("guards", {}).items()):
            for index, entry in enumerate(entries):
                guard = entry.get(field) if isinstance(entry, dict) else None
                if known_positive(guard) and not (incomplete_only and guard["status"] != "not_evaluated"):
                    rows.append({"exam_id": row["exam_id"], "config": config, "rep_index": index + 1,
                                 "violations": guard["violations"]})
    return rows


def decide(exams, *, decision_exam_ids, policy, complexity):
    """The acceptance table, first matching row wins. Pure: reads no files and keeps its inputs intact."""
    baseline, candidate = policy["baseline"], policy["candidate"]
    configs = (baseline, candidate)
    ratio = exact(policy["cost_ratio"])
    selected = list(dict.fromkeys(decision_exam_ids))
    by_id = {row["exam_id"]: row for row in exams}
    public, diffs = {}, {}
    for row in exams:
        scores = {config: list(row.get("scores", {}).get(config, [])) for config in configs}
        noise = noise_line(scores)
        diff = mean(scores[candidate]) - mean(scores[baseline]) if all(scores.values()) else None
        diffs[row["exam_id"]] = diff
        public[row["exam_id"]] = {"noise": noise["value"], "mean_diff": as_decimal(diff),
                                  "judgeable": row["exam_id"] in selected and bool(row.get("answers_present")) and noise["status"] == "ok"}
    known = known_violations(exams, "injection")
    judgeable = [key for key in selected if key in public and public[key]["judgeable"]]
    result = {"known_injection_violations": known, "exams": public, "judgeable_exams": judgeable,
              "known_constraint_violations": known_violations(exams, "constraint", incomplete_only=True),
              "decision_exams": selected, "warnings": [], "blockers": [], "constraint_rises": []}

    def finish(row, decision, preferred, reason):
        return {**result, "decision": decision, "preferred": preferred, "matched_row": row,
                "matched_rule": ROWS[row], "reason": reason}

    blockers = []
    if not selected:
        blockers.append("判定题总数为 0")
    if len(judgeable) * 2 < len(selected):
        blockers.append(f"可判断题 {len(judgeable)} 道，不到判定题 {len(selected)} 道的一半")
    for key in selected:
        row = by_id.get(key)
        if row is None or not row.get("answers_present"):
            blockers.append(f"{key} 缺答卷：{(row or {}).get('answers_reason') or '没有这道题的记录'}")
        else:
            blockers.extend(missing_evidence(row, configs))
    if blockers:
        result["blockers"] = blockers
        return finish(1, "indeterminate", None, "；".join(blockers))
    if known:
        return finish(2, "reject", baseline, f"注入违规非零，共 {sum(row['violations'] for row in known)} 次")

    # Not applicable means no constraint exists to break. Unknown and partial counts were stopped by the first row.
    per_exam = {key: {config: mean([entry["constraint"].get("violations") or 0 for entry in by_id[key]["guards"][config]])
                      for config in configs} for key in selected}
    violations = {config: sum(per_exam[key][config] for key in selected) for config in configs}
    costs = {config: sum(mean(by_id[key]["cost_usd"][config]) for key in selected) for config in configs}
    result["totals"] = {"constraint_violations": {key: as_decimal(value) for key, value in violations.items()},
                        "cost_usd": {key: as_decimal(value) for key, value in costs.items()},
                        "cost_ratio_limit": as_decimal(ratio)}
    # Exam by exam: a drop on one exam must not pay for a rise on another. Cost stays a total, below.
    rises = [{"exam_id": key, "baseline": as_decimal(per_exam[key][baseline]), "candidate": as_decimal(per_exam[key][candidate])}
             for key in selected if per_exam[key][candidate] > per_exam[key][baseline]]
    result["constraint_rises"] = rises
    if rises:
        named = "；".join(f"{row['exam_id']}（{candidate} 平均 {text_value(row['candidate'])} 次，"
                         f"{baseline} 平均 {text_value(row['baseline'])} 次）" for row in rises)
        return finish(3, "reject", baseline, f"硬约束违规增加：{named}")
    # A zero baseline makes the ratio undefined: an equal zero is no rise, anything above it exceeds the limit.
    if costs[candidate] > (costs[baseline] * ratio):
        return finish(3, "reject", baseline, f"成本超过基线的 {as_decimal(ratio)} 倍")
    better = [key for key in judgeable if diffs[key] > exact(public[key]["noise"])]
    worse = [key for key in judgeable if diffs[key] < -exact(public[key]["noise"])]
    if len(better) * 2 > len(judgeable):
        return finish(4, "accept", candidate, f"{len(better)} / {len(judgeable)} 道可判断题的均值差超过噪声线")
    if len(worse) * 2 > len(judgeable):
        return finish(5, "reject", baseline, f"{len(worse)} / {len(judgeable)} 道可判断题的均值差低于负噪声线")
    sizes = {config: complexity.get(config) for config in configs}
    if None in sizes.values():
        result["warnings"].append("有配置没写 complexity，选不出更简单的版本")
        return finish(6, "prefer_simpler", None, "没有明显变好，也没有明显变坏；缺 complexity，选不出版本")
    # A tie keeps the existing version: not changing anything is the simplest move.
    preferred = candidate if sizes[candidate] < sizes[baseline] else baseline
    return finish(6, "prefer_simpler", preferred, "没有明显变好，也没有明显变坏，选更简单的版本")


def main_scores(key, verdicts):
    """Score the frozen sample in key["units"]; config_reps only says who produced a mechanism."""
    units = key["units"]
    sizes = {len(rows) for rows in units.values()}
    if len(sizes) > 1:
        raise ValueError("Conservation failed: units have different item counts")
    result, warnings = {}, []
    for name, rows in units.items():
        lacking = [mechanism for mechanism in rows if mechanism not in verdicts]
        if lacking:
            raise ValueError(f"Conservation failed: scored mechanisms have no verdict: {lacking}")
        result[name] = sum(scored(verdicts[mechanism]) for mechanism in rows)
    per_unit = next(iter(sizes), 0)
    if per_unit < MIN_UNIT_SAMPLE:
        warnings.append(f"每个单元只有 {per_unit} 条，少于 {MIN_UNIT_SAMPLE} 条，样本太少")
    return {"units": result, "per_unit": per_unit, "warnings": warnings}


def unit_reps(root, exam_id, config):
    """A config this exam never ran has no units; that is an answer, not an error."""
    if not (Path(root) / exam_id / config).is_dir():
        return []
    return [rep for _, rep in discover_units(root, exam_id, config)]


def split_units(units):
    grouped = {}
    for name, value in units.items():
        config, rep = name.rsplit("/rep-", 1)
        grouped.setdefault(config, {})[int(rep)] = value
    return grouped


def is_revealed(path):
    return (path / "revealed.json").exists()


def sheet_keys(root):
    # ceiling: reparses every key per call, a few times per exam; pass one snapshot around past hundreds of sheets.
    return [(path.parent, read_file(path)) for path in sorted((Path(root) / "sheets").glob(f"*/{KEY_NAME}"))]


def find_compare_sheet(root, exam_id, configs, retired=()):
    """Exactly one revealed comparison sheet over exactly the compared configs; never guess between several."""
    wanted = set(configs)
    matches, unrevealed, outdated = [], 0, 0
    for path, key in sheet_keys(root):
        if key.get("exam_id") != exam_id or key.get("view") != "compare":
            continue
        if set(split_units(key.get("units", {}))) != wanted:
            continue
        if set(key["units"]) & set(retired):
            outdated += 1
        elif is_revealed(path):
            matches.append(key)
        else:
            unrevealed += 1
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, f"有 {len(matches)} 张已还原的比较卷，不猜用哪张"
    if outdated:
        return None, f"有 {outdated} 张比较卷还算着已被替换的单元，要重新出一张"
    return None, f"没有已还原的比较卷（未还原 {unrevealed} 张）"


def valid_verdict(row):
    return (isinstance(row, dict) and isinstance(row.get("idea_id"), str) and type(row.get("violation")) is bool
            and isinstance(row.get("reason"), str) and (bool(row["reason"].strip()) or not row["violation"]))


def constraint_guard(result, rows):
    """Accept a judge result only when every row is valid and covers exactly this unit.

    The file comes from disk, past the parser: one bad row spoils the whole result, known positives stay.
    Whether constraints apply at all is for the exam to say, see unit_constraint, never for this file.
    """
    if result is None:
        return {"status": "not_evaluated", "violations": None, "reason": "没有约束判定结果"}
    ids = sorted(row["idea_id"] for row in rows)
    produced = {row["idea_id"] for row in rows if row["level"] != "baseline"}
    raw = result.get("verdicts") if isinstance(result.get("verdicts"), list) else []
    verdicts = [row for row in raw if valid_verdict(row)]
    judged = sorted(row["idea_id"] for row in verdicts)
    positive = {row["idea_id"] for row in verdicts if row["violation"] and row["idea_id"] in produced}
    inputs = result.get("input_ids")
    if (result.get("status") != "checked" or not isinstance(inputs, list) or sorted(map(str, inputs)) != ids
            or len(verdicts) != len(raw) or judged != ids):
        return {"status": "not_evaluated", "violations": len(positive) or None,
                "reason": "约束判定没有恰好覆盖这个单元的全部点子"}
    return {"status": "checked", "violations": len(positive), "violation_scope": "nonbaseline_ideas"}


def judge_unit(run_dir, exam, config, rep, *, backend, ledger, model, max_output_tokens=DEFAULTS["max_output_tokens"]):
    path = output_dir(run_dir, exam["id"], config, rep)
    return evaluate(read_rows(path / "ideas.jsonl"), exam, path / "judges/constraint", backend=backend, ledger=ledger,
                    model=model, mode="constraint", max_output_tokens=max_output_tokens)


def unit_constraint(run_dir, exam, config, rep, rows):
    if not exam.get("constraints"):
        return {"status": "not_applicable", "violations": None}
    path = output_dir(run_dir, exam["id"], config, rep) / "judges/constraint/result.json"
    return constraint_guard(read_file(path) if path.is_file() else None, rows)


def project_subproblems(annotation, rows):
    if annotation is None:
        return None
    ids = {row["idea_id"] for row in rows if row["level"] != "baseline"}
    return {"exam_id": annotation["exam_id"], "targets": annotation["targets"],
            "matches": {key: value for key, value in annotation["matches"].items() if key in ids}}


def collect_unit(run_dir, exam, config, rep, *, annotation=None):
    """Always recompute from public projections; a stale metrics.json is never trusted."""
    # ceiling: load_unit rereads the whole exam per unit and save_metrics rewrites one file per unit, so a report
    # grows with the square of the units. Fine for dozens of units; share a loader before running hundreds.
    data = load_unit(run_dir, exam["id"], config, rep)
    result = compute_unit(data, exam_id=exam["id"], config=config, rep=rep, exam=exam,
                          subproblems=project_subproblems(annotation, data["ideas"]),
                          constraint=unit_constraint(run_dir, exam, config, rep, data["ideas"]))
    return data, result


def stage_id(*parts):
    return "_".join(re.sub(r"[^A-Za-z0-9]+", "_", part).strip("_") for part in parts)


def drop_changes(config, kind, value=None):
    """The one switch that removes a stage; a method or model may need two fields to stay a valid config."""
    if kind == "level":
        rest = [level for level in config["levels"] if level != value]
        return {"levels": rest or ["baseline"]}, None
    if kind == "return":
        return {"return_collisions": False}, None
    if kind == "method":
        rest = {name: text for name, text in config["methods"].items() if name != value}
        return ({"methods": rest} if rest else {"method_mode": "none", "methods": {}}), None
    if kind == "materials":
        return {"materials_mode": "none"}, None
    if kind == "model":
        rest = [model for model in config["models"] if model != value]
        if not rest:
            return None, "这个配置只有一个模型，不能去掉"
        return {"models": rest, **({"default_model": rest[0]} if config["default_model"] == value else {})}, None
    if kind == "switch":
        return {value: UNRECORDED_SWITCHES[value]}, None
    raise ValueError(f"Unknown stage kind: {kind}")


def leave_one_config(config, stage, kind, value=None):
    changes, reason = drop_changes(config, kind, value)
    if changes is None:
        return {"drop_config": None, "changed": {}, "reason": reason}
    # Budget and every other field stay: a comparison changes one structure at a time.
    built = validate_config({**deepcopy(config), **changes, "name": f"{config['name']}__drop_{stage}"})
    return {"drop_config": built, "changed": changes, "reason": None}


def generation_levels(config):
    return [level for level in LEVELS if level != "baseline" and level in config["levels"]]


def stages(config):
    produced = lambda row: row["level"] != "baseline"
    result = [(stage_id("level", level), f"级别 {level}", "level", level, lambda row, level=level: row["level"] == level)
              for level in generation_levels(config)]
    if config["return_collisions"]:
        result.append(("return", "撞车退回", "return", None, lambda row: produced(row) and row["returned"]))
    if config["method_mode"] != "none":
        result.extend((stage_id("method", name), f"方法 {name}", "method", name,
                       lambda row, name=name: produced(row) and row["method"] == name) for name in sorted(config["methods"]))
    if config["materials_mode"] != "none":
        result.append(("materials", "材料", "materials", None, lambda row: produced(row) and bool(row["cards_used"])))
    result.extend((stage_id("model", name), f"模型 {name}", "model", name,
                   lambda row, name=name: produced(row) and row["model"] == name) for name in sorted(config["models"]))
    return result


def contribution(config, ideas, table, verdicts, *, floor=DEFAULT_CONTRIBUTION_FLOOR):
    """How many scored mechanisms needed a stage. Correlation only: nothing here removed the stage."""
    if not verdicts:
        return {"status": "no_data", "reason": "还没有作者判断", "stages": [], "denominator": None}
    rows = [row for row in ideas.values() if row["config"] == config["name"]]
    groups, waiting = [], []
    for mechanism in table["mechanisms"]:
        verdict = verdicts.get(mechanism["mechanism_id"])
        members = [ideas[key] for key in mechanism["idea_ids"] if key in ideas and ideas[key]["config"] == config["name"]]
        if not members:
            continue
        if verdict is None:
            waiting.append(members)
        elif scored(verdict):
            groups.append(members)
    limit = exact(floor)
    output = []
    for stage, label, kind, value, passes in stages(config):
        present = sum(map(passes, rows))
        numerator = sum(all(map(passes, members)) for members in groups)
        share = Fraction(numerator, len(groups)) if groups else None
        # A sample leaves mechanisms unread. A stage whose work nobody judged yet has shown nothing, good or bad.
        unjudged = sum(all(map(passes, members)) for members in waiting)
        output.append({"stage": stage, "label": label, "kind": kind, "value": value, "present": present,
                       "numerator": numerator, "denominator": len(groups), "share": as_decimal(share), "unjudged": unjudged,
                       "suspect": bool(present and groups and share <= limit and not unjudged)})
    return {"status": "ok", "reason": None, "stages": output, "denominator": len(groups)}


def unrecorded_switches(config):
    active = []
    for name, off in UNRECORDED_SWITCHES.items():
        if config[name] != off:
            active.append({"stage": stage_id("switch", name), "label": name, "kind": "switch", "value": name,
                           "reason": "血统没有记录，算不了贡献度"})
    return active


def leave_one_row(config, entry, **extra):
    return {"config": config["name"], "stage": entry["stage"], "label": entry["label"], "exams": [], **extra,
            **leave_one_config(config, entry["stage"], entry["kind"], entry["value"])}


def leave_one_rows(configs, contributions):
    rows = {}
    for exam_id, per_config in contributions.items():
        for config in configs:
            for entry in per_config[config["name"]]["stages"]:
                if entry["suspect"]:
                    key = (config["name"], entry["stage"])
                    if key not in rows:
                        rows[key] = leave_one_row(config, entry)
                    rows[key]["exams"].append(exam_id)
    for config in configs:
        for entry in unrecorded_switches(config):
            rows[(config["name"], entry["stage"])] = leave_one_row(config, entry, note=entry["reason"])
    return [rows[key] for key in sorted(rows)]


def ledger_row(unit, symptom, status, count, denominator, idea_ids=(), **extra):
    return {**unit, "symptom": symptom, "stage": SYMPTOMS[symptom][1], "status": status, "count": count,
            "denominator": denominator, "idea_ids": sorted(idea_ids), **extra}


def no_data(unit, symptom, reason, **extra):
    return ledger_row(unit, symptom, "no_data", None, None, reason=reason, **extra)


def failure_rows(unit, rows, table, fallback_ids, exhaustion, levels, annotation, completion=None):
    """One row per unit and symptom. Without evidence the row says so; it never reports a zero."""
    if completion is not None and completion["status"] != "ok":
        return [no_data(unit, symptom, completion.get("reason") or "单元不完整") for symptom in AUTOMATIC_SYMPTOMS]
    produced = [row for row in rows if row["level"] != "baseline"]
    output = []
    assigned = [row for row in produced if row["cards"]]
    if assigned:
        hits = [row["idea_id"] for row in assigned if not row["cards_used"]]
        output.append(ledger_row(unit, "unused_cards", "ok", len(hits), len(assigned), hits))
    else:
        output.append(no_data(unit, "unused_cards", "没有分到卡片的非基线点子"))
    returned = [row for row in produced if row["returned"]]
    if table is None or not returned:
        output.append(no_data(unit, "return_same_mechanism", "没有机制表" if table is None else "这个单元没有退回的点子",
                              unknown_ids=[]))
    else:
        membership = {key: group["mechanism_id"] for group in table["mechanisms"] for key in group["idea_ids"]}
        unsure = set(fallback_ids)
        known, unknown, hits = [], [], []
        for row in returned:
            pair = (row["idea_id"], row["returned_from"])
            if set(pair) & unsure or any(key not in membership for key in pair):
                unknown.append(row["idea_id"])
                continue
            known.append(row["idea_id"])
            if membership[pair[0]] == membership[pair[1]]:
                hits.append(row["idea_id"])
        if known:
            output.append(ledger_row(unit, "return_same_mechanism", "ok", len(hits), len(known), hits, unknown_ids=sorted(unknown)))
        else:
            output.append(no_data(unit, "return_same_mechanism", "退回的点子全部归属未知", unknown_ids=sorted(unknown)))
    observed = {level: exhaustion[level] for level in levels if level in exhaustion}
    measured = {level: entry for level, entry in observed.items() if entry["status"] == "ok"}
    unmeasured = {level: entry.get("reason") for level, entry in observed.items() if entry["status"] != "ok"}
    if measured:
        flat = sorted(level for level, entry in measured.items() if entry["value"] == 0)
        output.append(ledger_row(unit, "level_no_new", "ok", len(flat), len(measured), levels=flat, not_evaluated=unmeasured))
    else:
        output.append(no_data(unit, "level_no_new", "没有可用的级别增长证据", not_evaluated=unmeasured))
    if annotation is None:
        output.append(no_data(unit, "subproblem_empty", "没有独立的子问题标注"))
    else:
        matches = annotation["matches"]
        ids = {row["idea_id"] for row in produced}
        if not ids or not ids <= matches.keys():
            output.append(no_data(unit, "subproblem_empty", "子问题标注没有覆盖全部点子"))
        else:
            hit = {target for key in ids for target in matches[key]}
            empty = sorted(set(annotation["targets"]) - hit)
            output.append(ledger_row(unit, "subproblem_empty", "ok", len(empty), len(annotation["targets"]), targets=empty,
                                     simulated=bool(annotation.get("simulated"))))
    return output


def hide_idea_ids(rows):
    """Lineage per idea stays hidden while the author is still answering on this exam."""
    for row in rows:
        row["idea_ids_hidden"] = bool(row["idea_ids"] or row.get("unknown_ids"))
        row["idea_ids"] = []
        if "unknown_ids" in row:
            row["unknown_ids"] = []


def manual_rows(calibrations):
    rows = []
    for symptom in MANUAL_SYMPTOMS:
        row = ledger_row({"exam_id": None, "config": None, "rep": None}, symptom, "manual", None, None)
        if symptom == "dedupe_false_merge":
            row["reference"] = {exam: entry.get("difference_counts") for exam, entry in calibrations.items()}
        rows.append(row)
    return rows


def rule_self_check(results, threshold=DEFAULT_CHECK_BAD_RATIO):
    """Does the scoring rule stop nonsense? Repeats compare questions two and three only, in check_results."""
    bad_total = sum(row["check_bad"]["total"] for row in results.values())
    bad_scored = sum(row["check_bad"]["scored"] for row in results.values())
    repeats = {key: sum(row["repeat"][key] for row in results.values()) for key in ("total", "consistent", "inconsistent")}
    limit = exact(threshold)
    output = {"threshold": as_decimal(limit), "threshold_status": "暂定", "sheets": results, "repeat": repeats,
              "check_bad": {"total": bad_total, "scored": bad_scored}, "warning": None,
              "status": "ok" if bad_total else "no_data", "ratio": None}
    if bad_total:
        ratio = Fraction(bad_scored, bad_total)
        output["ratio"] = as_decimal(ratio)
        if ratio > limit:
            output["warning"] = f"{bad_scored} / {bad_total} 条明显不行的样本被计分，超过阈值，规则可能挡不住胡话"
    return output


def want_summary(table, verdicts):
    """Recorded separately: wanting to try never reaches the main score."""
    if not verdicts:
        return {"status": "no_data", "wanted": None, "total": None, "ratio": None, "scored_not_wanted": []}
    wanted = sum(row["want_to_try"] for row in verdicts.values())
    labels = {row["mechanism_id"]: row["label"] for row in table["mechanisms"]}
    picked = [{"mechanism_id": key, "label": labels[key]} for key, row in sorted(verdicts.items())
              if scored(row) and not row["want_to_try"]]
    return {"status": "ok", "wanted": wanted, "total": len(verdicts), "ratio": as_decimal(Fraction(wanted, len(verdicts))),
            "scored_not_wanted": picked}


def expansion(table, ideas, verdicts):
    """Only mechanisms the author already judged: everything else would break the blind."""
    fields = ("idea_id", "config", "rep", "agent", "level", "model", "method", "returned", "title", "text")
    if not verdicts:
        return []
    rows = []
    for mechanism in table["mechanisms"]:
        verdict = verdicts.get(mechanism["mechanism_id"])
        if verdict is None:
            continue
        rows.append({"mechanism_id": mechanism["mechanism_id"], "label": mechanism["label"], "scored": scored(verdict),
                     "want_to_try": verdict["want_to_try"], "fatal_reason": verdict["fatal_reason"],
                     "members": [{field: ideas[key][field] for field in fields} for key in mechanism["idea_ids"]]})
    return rows


def verify_counts(run_dir, exam_id):
    """Recount every score sheet of the exam. An inequality stops the report, it is not a warning."""
    root = Path(run_dir).resolve()
    ideas = load_ideas(root, safe_segment(exam_id))
    table_path = root / exam_id / "mechanisms.json"
    current = {row["mechanism_id"] for row in read_file(table_path)["mechanisms"]} if table_path.is_file() else set()
    output = {"exam_id": exam_id, "ideas": len(ideas), "mechanisms": len(current), "sheets": [], "new_after_issue": []}
    for path, key in sheet_keys(root):
        if key.get("exam_id") != exam_id or key.get("kind") != "score":
            continue
        name = path.name
        if not (path / "sheet.json").is_file():
            raise ValueError(f"Conservation failed: {name} has a key but no sheet.json")
        sheet = read_file(path / "sheet.json")
        if {item["item_id"] for item in sheet["items"]} != set(key["items"]):
            raise ValueError(f"Conservation failed: {name} key and sheet.json hold different items")
        record = load_issued(path)
        check_issued(record, key, [item["item_id"] for item in sheet["items"]])
        counts = key["counts"]
        if len(sheet["items"]) != counts["presented_target"] + counts["check_bad"] + counts["repeat"]:
            raise ValueError(f"Conservation failed: {name} sheet.json item count differs from counts")
        check_conservation(key["view"], key["items"], counts, key["reused"])
        selected = set(record["selected"])
        if selected - current:
            raise ValueError(f"Conservation failed: {name} names mechanisms missing from mechanisms.json")
        row = {"sheet_id": name, "view": key["view"], "counts": counts, "masked_items": sum(MASK in item["text"] for item in sheet["items"]),
               "revealed": is_revealed(path)}
        if key["view"] == "all":
            # The issued record fixed the full set, so anything else in the table really did arrive after this sheet.
            if len(selected) != counts["mechanisms_total"]:
                raise ValueError(f"Conservation failed: {name} presented_target + reused != mechanisms_total")
            row["new_after_issue"] = sorted(current - selected)
        output["sheets"].append(row)
    views = [row for row in output["sheets"] if row["view"] == "all"]
    output["status"] = "ok" if views else "no_data"
    if views:
        # Mechanisms only grow, so the widest full view is the latest one.
        latest = max(views, key=lambda row: (row["counts"]["mechanisms_total"], row["counts"]["reused"]))
        output.update({field: latest["counts"][field] for field in ("presented_target", "reused", "mechanisms_total")},
                      new_after_issue=latest["new_after_issue"])
    return output


def load_receipts(root):
    # ceiling: scans the run tree per report, and a config literally named dedupe or judges would be misfiled here.
    rows = []
    root = Path(root)
    for path in sorted(root.rglob("calls.jsonl")):
        parts = path.relative_to(root).parts
        if parts[0] == "replay":
            # Calibration and replay share the directory; a partition is named after its run plus a suffix.
            scope = "calibration" if (root / "replay" / parts[1].split("__")[0] / "calibration.json").is_file() else "replay"
        else:
            scope = ("sheet_rewrite" if parts[0] == "sheets" else "constraint" if "judges" in parts
                     else "dedupe" if "dedupe" in parts else "recording")
        rows.extend({**row, "_scope": scope} for row in read_rows(path))
    return rows


def read_ledger(path):
    """Read the raw events without opening BudgetLedger, whose recovery could write."""
    return {row["call_key"]: row for row in read_rows(path)}


def usd_sum(rows):
    """Known amounts only; a caller that must not ignore an unknown one checks for it first."""
    return exact_sum(0, *(amount(row["usd"]) for row in rows if row.get("usd") is not None))


def reconcile(receipts, events, *, exact_match=False):
    """Receipts must be settled ledger entries. A real ledger spans runs, so only a fake one must match fully."""
    keys = Counter(row["call_key"] for row in receipts)
    settled = {key: row for key, row in events.items() if row["event"] == "settle"}
    by_key = {row["call_key"]: row for row in receipts}
    unsettled = sorted(key for key, row in by_key.items() if key not in settled and row["outcome"] != "uncertain")
    differ = sorted(key for key, row in by_key.items() if key in settled and amount(row["usd"]) != amount(settled[key]["usd"]))
    unreceipted = sorted(set(settled) - set(by_key)) if exact_match else []
    duplicate = sorted(key for key, number in keys.items() if number > 1)
    return {"ok": not (unsettled or differ or unreceipted or duplicate), "mode": "exact" if exact_match else "subset",
            "receipts": len(by_key), "settled": len(settled), "unsettled_receipts": unsettled, "amount_mismatch": differ,
            "settled_without_receipt": unreceipted, "duplicate_receipts": duplicate,
            "receipts_usd": usd_sum(by_key.values()), "settled_usd": usd_sum(settled.values()),
            "open_events": sorted(key for key, row in events.items() if row["event"] in ("reserve", "uncertain"))}


def cost_summary(receipts):
    def bucket(rows):
        errors = [row for row in rows if row["outcome"] == "paid_error"]
        return {"calls": len(rows), "usd": usd_sum(rows), "estimated_usd": usd_sum(row for row in rows if row.get("usd_estimated")),
                "paid_error_calls": len(errors), "paid_error_usd": usd_sum(errors),
                "uncertain_calls": sum(row["outcome"] == "uncertain" for row in rows)}
    group = lambda field: {name: bucket([row for row in receipts if row[field] == name])
                           for name in sorted({row[field] for row in receipts})}
    return {"total": bucket(receipts), "by_purpose": group("purpose"), "by_stage": group("_scope"),
            "backends": sorted({row["backend"] for row in receipts})}


def compact_metrics(metrics):
    """Value and status only: details such as graph edges name ideas, which an open sheet must not see."""
    return {name: {key: value for key, value in result.items() if key in ("value", "status", "reason")}
            for name, result in metrics.items()}


def compact_unit(result):
    return {**{key: result[key] for key in ("unit", "counts", "guards", "costs", "completion")},
            "metrics": compact_metrics(result["metrics"]), "exhaustion": compact_metrics(result["exhaustion"])}


def compact_side(entry):
    metrics = compact_metrics(entry["metrics"])
    return {"ideas": len(entry["idea_ids"]), "calls": len(entry["call_keys"]), "tokens": entry.get("tokens"),
            "usd": entry["costs"]["usd"]["value"], "metrics": metrics}


def compact_comparison(comparison):
    """Selections name idea IDs, which an open sheet must not see; the report keeps only the aggregates."""
    output = {}
    for mode in ("same_ideas", "same_tokens"):
        entry = comparison[mode]
        output[mode] = {key: entry[key] for key in ("status", "reason", "requested", "cap", "available",
                                                    "value", "token_difference") if key in entry}
        for side in ("a", "b"):
            if side in entry and "metrics" in entry[side]:
                output[mode][side] = compact_side(entry[side])
    return output


def pair_evidence(root, exam_id, pair, verdicts, guards, spent, retired):
    """What the acceptance table reads for one pair of configs on one exam."""
    key, reason = find_compare_sheet(root, exam_id, pair, retired)
    scores, warnings, per_unit = {}, [], None
    if key is not None:
        scoring = main_scores(key, verdicts)
        warnings, per_unit = scoring["warnings"], scoring["per_unit"]
        for name, values in split_units(scoring["units"]).items():
            scores[name] = [values[rep] for rep in sorted(values)]
    return {"exam_id": exam_id, "answers_present": key is not None, "answers_reason": reason,
            "scores": scores, "guards": guards, "cost_usd": spent}, warnings, per_unit


def exam_report(root, exam, configs, *, baseline, candidate, seed, floor, removals=()):
    exam_id = exam["id"]
    annotation_path = root / exam_id / "subproblems.json"
    annotation = read_file(annotation_path) if annotation_path.is_file() else None
    ideas = load_ideas(root, exam_id)
    table = load_table(root, exam_id, ideas) if (root / exam_id / "mechanisms.json").is_file() else None
    verdicts = load_verdicts(root, exam_id, table) if table is not None else {}
    swaps = replacements.load(root, exam_id)
    retired = {unit_name(row["config"], row["failed_rep"]) for row in swaps}
    # Diagnostics read the same population as the score: a replaced unit is not part of either.
    counted = {key: row for key, row in ideas.items() if unit_name(row["config"], row["rep"]) not in retired}
    units, failures, guards, spent = {}, [], {}, {}
    comparisons, datasets = {}, {}
    open_sheets = [path.name for path, key in sheet_keys(root)
                   if key.get("exam_id") == exam_id and not is_revealed(path)]
    for config in configs:
        name = config["name"]
        for rep in unit_reps(root, exam_id, name):
            data, result = collect_unit(root, exam, name, rep, annotation=annotation)
            datasets[(name, rep)] = (data, result)
            units.setdefault(name, {})[rep] = compact_unit(result)
            guards.setdefault(name, []).append(result["guards"])
            cost = result["costs"]["source_recording"]["usd"]
            spent.setdefault(name, []).append(cost["value"] if cost["status"] == "ok" else None)
            failures.extend(failure_rows({"exam_id": exam_id, "config": name, "rep": rep}, data["ideas"],
                table, data["fallback_ids"], result["exhaustion"],
                generation_levels(config), annotation, completion=result["completion"]))
    slots = {name: {replacements.slot(swaps, name, rep): rep for unit, rep in datasets if unit == name} for name in (baseline, candidate)}
    for slot in sorted(slots[baseline].keys() & slots[candidate].keys()):
        first, second = slots[baseline][slot], slots[candidate][slot]
        data, result = datasets[(baseline, first)]
        result["comparisons"] = compare_units(data, datasets[(candidate, second)][0], seed=seed, replaced=first != second)
        comparisons[slot] = compact_comparison(result["comparisons"])
    for _, result in datasets.values():
        save_metrics(root, result)
    evidence, warnings, per_unit = pair_evidence(root, exam_id, (baseline, candidate), verdicts, guards, spent, retired)
    if open_sheets:
        hide_idea_ids(failures)
    tested = {}
    for parent, removed in removals:
        pair = (parent, removed)
        # Only this pair's guards: a violation in some other removal must not decide this one.
        tested[removed] = pair_evidence(root, exam_id, pair, verdicts, {name: guards.get(name, []) for name in pair},
                                        {name: spent.get(name, []) for name in pair}, retired)[0]
    return {"evidence": evidence, "removal_evidence": tested, "per_unit": per_unit,
            "warnings": warnings, "units": units, "failures": failures, "comparisons": comparisons,
            # The classifier state is per exam, so every unit of the exam reports the same fallback list.
            "open_sheets": open_sheets, "fallback_ids": len(next(iter(datasets.values()))[0]["fallback_ids"]) if datasets else 0,
            "contribution": {config["name"]: contribution(config, counted, table, verdicts, floor=floor) for config in configs},
            "want": want_summary(table, verdicts), "expansion": expansion(table, ideas, verdicts),
            "unjudged_count": len(table["mechanisms"]) - len(verdicts) if table else None,
            "counts": verify_counts(root, exam_id)}


def calibration_rows(root, exam_ids):
    output = {}
    for path in sorted((root / "replay").glob("*/calibration.json")):
        report = read_file(path)
        if report.get("exam_id") in exam_ids:
            # Calibration holds idea text; only the aggregates may reach a report the author reads mid-run.
            output[report["exam_id"]] = {key: report.get(key) for key in ("status", "models", "ami", "difference_counts",
                                                                         "fallback_counts")}
    return output


def replay_rows(root, exam_ids):
    """Status per size: a run can finish while a size it could not sample stays unknown."""
    output = {}
    for path in sorted((root / "replay").glob("*/result.json")):
        result = read_file(path)
        if result["unit"]["exam_id"] not in exam_ids:
            continue
        samples = {size: ({"status": "ok", "metrics": compact_metrics(entry["metrics"])} if "metrics" in entry else
                          {key: entry.get(key) for key in ("status", "reason", "requested", "available")})
                   for size, entry in result["samples"].items()}
        output.setdefault(result["unit"]["exam_id"], []).append(
            {"name": result["name"], "unit": result["unit"], "status": result["status"],
             "generation_calls": result["generation_calls"], "samples": samples})
    return output


def self_check_results(root):
    results = {}
    for path, key in sheet_keys(root):
        if key.get("kind") == "score" and is_revealed(path):
            results[path.name] = check_results(root, path.name)
    return results


def text_value(value):
    if value is None:
        return "—"
    if isinstance(value, (Decimal, float)):
        return f"{Decimal(str(value)):.4f}".rstrip("0").rstrip(".")
    return str(value)


def metric_text(entry):
    return "未评估" if entry["status"] != "ok" else text_value(entry["value"])


def nothing(reason):
    return [f"无数据：{reason}"]


def md_table(header, rows):
    cells = lambda row: " | ".join(str(cell).replace("|", "\\|") for cell in row)
    return [f"| {cells(header)} |", f"|{':---|' * len(header)}", *(f"| {cells(row)} |" for row in rows)]


def money(value):
    """Exact, unlike text_value: a small real charge must not round away to nothing."""
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def guard_text(guard):
    violations = guard.get("violations")
    if guard["status"] == "not_applicable":
        return "不适用"
    if guard["status"] == "not_evaluated":
        return "未评估" + (f"（已知 {violations} 次）" if violations else "")
    return f"已查，{violations} 次"


def section_decision(report):
    decision = report["decision"]
    lines = [f"**判定：`{decision['decision']}`**（命中第 {decision['matched_row']} 行：{decision['reason']}）",
             f"- 推荐版本：{decision['preferred'] or '无'}",
             f"- {SAMPLE_NOTE}"]
    if "fake" in report["costs"]["backends"]:
        lines.append("- 这次用了假后端：这个判定只证明接线，不是结论。")
    if decision["known_constraint_violations"]:
        total = sum(row["violations"] for row in decision["known_constraint_violations"])
        lines.append(f"- 硬约束只判了一部分，已知 {total} 次违规，实际可能更多。")
    if decision["known_injection_violations"]:
        total = sum(row["violations"] for row in decision["known_injection_violations"])
        lines.append(f"- **注入告警：已知 {total} 次注入违规。** 明细见第 3 节。")
    lines.append(f"- 未还原的卷子：{report['unrevealed_sheets']} 张。")
    lines.extend(f"- 警告：{text}" for text in [*decision["warnings"], *report["warnings"]])
    return lines


def section_scores(report):
    rows = []
    baseline, candidate = report["policy"]["baseline"], report["policy"]["candidate"]
    for exam_id, entry in report["exams"].items():
        verdict = report["decision"]["exams"][exam_id]
        scores = entry["evidence"]["scores"]
        rows.append([exam_id, ", ".join(map(str, scores.get(baseline, []))) or "无数据",
                     ", ".join(map(str, scores.get(candidate, []))) or "无数据",
                     text_value(verdict["noise"]) if verdict["noise"] is not None else "无数据（不足 3 次）",
                     text_value(verdict["mean_diff"]) if verdict["mean_diff"] is not None else "无数据",
                     "是" if verdict["judgeable"] else "否",
                     entry["evidence"]["answers_reason"] or "；".join(entry["warnings"]) or "—"])
    if not rows:
        return nothing("运行目录里没有考题")
    return [f"比较：`{baseline}` 对 `{candidate}`，均值差 = 后者减前者。",
            *md_table(["题", baseline, candidate, "噪声线", "均值差", "可判断", "备注"], rows)]


def section_guards(report):
    rows = []
    for exam_id, entry in report["exams"].items():
        for config, reps in entry["units"].items():
            for rep, result in sorted(reps.items()):
                cost = result["costs"]["source_recording"]["usd"]
                rows.append([exam_id, config, str(rep), guard_text(result["guards"]["injection"]),
                             guard_text(result["guards"]["constraint"]), metric_text(cost)])
    if not rows:
        return nothing("没有录制单元")
    lines = md_table(["题", "配置", "重复", "注入", "硬约束", "录制费用（美元）"], rows)
    totals = report["decision"].get("totals")
    if totals:
        lines.extend(["", *constraint_lines(report["decision"]), f"判定题汇总（各题各配置均值相加，硬约束这项只作参考）：硬约束 {dict_text(totals['constraint_violations'])}；"
                          f"费用 {dict_text(totals['cost_usd'])}；费用上限是基线的 {text_value(totals['cost_ratio_limit'])} 倍（暂定）。",
                      "费用只含录制，不含去重、改写和约束判定：那些按题共用，分不到配置，见第 10 节。"])
    return lines


def constraint_lines(decision):
    lines = ["硬约束按单题判定：任何一道判定题上，候选的违规均值高于基线就拒绝。"]
    for row in decision["constraint_rises"]:
        lines.append(f"- 硬约束变差的题：{row['exam_id']}，基线平均 {text_value(row['baseline'])} 次，候选平均 {text_value(row['candidate'])} 次。")
    return lines


def dict_text(values):
    return "，".join(f"{key} = {text_value(value)}" for key, value in values.items())


def section_contribution(report):
    lines = [CONTRIBUTION_NOTE]
    any_data = False
    for exam_id, entry in report["exams"].items():
        for config, result in entry["contribution"].items():
            if result["status"] != "ok":
                continue
            any_data = True
            rows = [[row["label"], str(row["present"]), f"{row['numerator']} / {row['denominator']}", text_value(row["share"]),
                     "可疑" if row["suspect"] else f"未判定 {row['unjudged']} 条" if row["unjudged"] else "—"] for row in result["stages"]]
            lines.extend(["", f"**{exam_id} / {config}**", "", *md_table(["环节", "点子数", "必须经过 / 计分机制", "占比", "判断"], rows)])
    for config in report["configs"]:
        for entry in unrecorded_switches(config):
            lines.append(f"- {config['name']} 的 `{entry['label']}` 开着：{entry['reason']}。")
    if not any_data:
        return [lines[0], *nothing("还没有作者判断")]
    lines.append(f"\n占比不高于 {text_value(report['policy']['contribution_floor'])}（暂定）算可疑。")
    return lines


def section_leave_one(report):
    waiting = [f"- `{config}` 的 {row['label']}（{exam_id}）：未判定 {row['unjudged']} 条，判完再看，现在不算可疑。"
               for exam_id, entry in report["exams"].items() for config, result in entry["contribution"].items()
               for row in result["stages"] if row["unjudged"]]
    if not report["leave_one"] and not waiting:
        return nothing("没有可疑环节，也没有打开血统不记录的开关")
    lines = []
    for row in report["leave_one"]:
        where = "、".join(row["exams"]) or "所有题"
        if row["drop_config"] is None:
            lines.append(f"- `{row['config']}` 的 {row['label']}（{where}）：{row['reason']}。")
        else:
            basis = row.get("note") or "贡献度可疑"
            lines.append(f"- `{row['config']}` 的 {row['label']}（{where}，{basis}）：用配置 `{row['drop_config']['name']}`，"
                         f"改动 `{json.dumps(row['changed'], ensure_ascii=False, sort_keys=True)}`。完整配置在 report.json。")
    return [*lines, *waiting]


def section_failures(report):
    rows = []
    for row in report["failures"]:
        label = SYMPTOMS[row["symptom"]][0]
        if row["status"] == "manual":
            note = "要人看"
            if row.get("reference"):
                note = "校准差异可参考：" + json.dumps(row["reference"], ensure_ascii=False, sort_keys=True)
            rows.append(["全部", "—", "—", label, row["stage"], "手工", "—", "—", note])
            continue
        missing = row["status"] == "no_data"
        count = "无数据" if missing else str(row["count"])
        denominator = "无数据" if missing else str(row["denominator"])
        notes = [row.get("reason") or ""]
        if row.get("unknown_ids"):
            notes.append(f"归属未知 {len(row['unknown_ids'])} 条")
        if row.get("levels"):
            notes.append("、".join(row["levels"]))
        if row.get("targets"):
            notes.append("、".join(row["targets"]))
        if row.get("simulated"):
            notes.append("模拟标注")
        rows.append([row["exam_id"], row["config"], str(row["rep"]), label, row["stage"],
                     "无数据" if missing else "已查", count, denominator,
                     "；".join(filter(None, notes)) or "—"])
    return md_table(["题", "配置", "重复", "现象", "指向环节", "状态", "条数", "分母", "备注"], rows)


def section_self_check(report):
    check = report["self_check"]
    lines = [f"告警阈值：检查样本计分比例超过 {text_value(check['threshold'])}（暂定）。"]
    if check["status"] == "no_data":
        lines.extend(nothing("没有已还原卷子里的检查样本"))
    else:
        lines.append(f"检查样本 {check['check_bad']['total']} 条，被计分 {check['check_bad']['scored']} 条，"
                     f"比例 {text_value(check['ratio'])}。")
        if check["warning"]:
            lines.append(f"**警告：{check['warning']}。**")
    repeat = check["repeat"]
    lines.append(f"重复项 {repeat['total']} 条，一致 {repeat['consistent']} 条，不一致 {repeat['inconsistent']} 条（只比第二、第三问）。"
                 if repeat["total"] else "重复项无数据。")
    return lines


def section_want(report):
    lines = ["想不想试只作记录，不进主分。"]
    picked = []
    has_data = False
    for exam_id, entry in report["exams"].items():
        want = entry["want"]
        if want["status"] == "no_data":
            lines.append(f"- {exam_id}：无数据。")
            continue
        has_data = True
        lines.append(f"- {exam_id}：想试 {want['wanted']} / {want['total']}，比例 {text_value(want['ratio'])}。")
        picked.extend(f"- {exam_id} {row['mechanism_id']}：{row['label']}" for row in want["scored_not_wanted"])
    lines.extend(["", "**计了分但作者不想试（供真试抽样）：**", *(picked or (nothing("还没有作者判断") if not has_data else ["没有这样的条目"]))])
    return lines


def section_metrics(report):
    lines = ["字符二元组 TF-IDF 是词面粗估，不代表语义覆盖。未评估的项不是 0。"]
    reasons = set()
    any_unit = False
    for exam_id, entry in report["exams"].items():
        for config, reps in entry["units"].items():
            any_unit = True
            names = list(next(iter(reps.values()))["metrics"])
            rows = []
            for name in names:
                cells = [metric_text(reps[rep]["metrics"][name]) for rep in sorted(reps)]
                reasons.update(reps[rep]["metrics"][name].get("reason", "") for rep in reps)
                rows.append([name, *cells])
            lines.extend(["", f"**{exam_id} / {config}**", "", *md_table(["指标", *(f"重复 {rep}" for rep in sorted(reps))], rows)])
        for rep, comparison in sorted(entry["comparisons"].items()):
            stand_ins = [f"{row['config']} 用替换后的重复 {row['new_rep']}" for row in report.get("replacements", [])
                         if row["exam_id"] == exam_id and str(row["failed_rep"]) == str(rep)]
            note = f"（{'；'.join(stand_ins)}）" if stand_ins else ""
            lines.extend(["", f"**{exam_id} 重复 {rep} 的两种比较口径{note}**", ""])
            for mode, label in (("same_ideas", "相同点子数"), ("same_tokens", "相同 token")):
                row = comparison[mode]
                if row["status"] != "ok":
                    lines.append(f"- {label}：未评估（{row.get('reason')}）")
                    continue
                sides = "；".join(f"{side}：{row[side]['ideas']} 条、{row[side]['calls']} 次调用、"
                                  f"独有机制 {metric_text(row[side]['metrics']['unique_mechanisms'])}"
                                  for side in ("a", "b"))
                lines.append(f"- {label}：{sides}" + (f"；上限 {row['cap']} token" if mode == "same_tokens" else ""))
    if not any_unit:
        return [lines[0], *nothing("没有录制单元")]
    lines.extend(["", "**回放**：只用录好的点子重跑下游，不是改变生成策略的反事实。", ""])
    for exam_id, rows in report["replay"].items():
        for row in rows:
            unit = row["unit"]
            lines.append(f"- {exam_id} / {unit['config']} 重复 {unit['rep']}：{row['status']}，生成调用 {row['generation_calls']} 次。")
            for size, sample in row["samples"].items():
                result = (f"独有机制 {metric_text(sample['metrics']['unique_mechanisms'])}" if sample["status"] == "ok"
                          else f"未评估（{sample.get('reason')}）")
                lines.append(f"  - {size} 个 agent：{result}")
    if not report["replay"]:
        lines.extend(nothing("没有回放结果"))
    reasons.discard("")
    if reasons:
        lines.extend(["", "未评估的原因：", *(f"- {reason}" for reason in sorted(reasons))])
    return lines


def section_costs(report):
    costs, check = report["costs"], report["receipts"]
    if not costs["total"]["calls"]:
        return nothing("没有调用收据")
    def cost_row(label, entry):
        return [label, str(entry["calls"]), str(entry["usd"]), str(entry["estimated_usd"]),
                f"{entry['paid_error_calls']} 次 / {entry['paid_error_usd']}", str(entry["uncertain_calls"])]
    rows = [cost_row(f"{title} {name}", entry) for title, group in (("用途", costs["by_purpose"]), ("阶段", costs["by_stage"]))
            for name, entry in group.items()]
    rows.append(cost_row("合计", costs["total"]))
    lines = md_table(["范围", "调用", "费用（美元）", "其中估算", "付费失败", "未决"], rows)
    fallback = {exam_id: entry["fallback_ids"] for exam_id, entry in report["exams"].items()}
    lines.extend(["", f"收据和账本（{check['mode']}）：{'一致' if check['ok'] else '不一致'}，收据 {check['receipts']} 张 "
                      f"{check['receipts_usd']} 美元，账本结算 {check['settled']} 笔 {check['settled_usd']} 美元。",
                  f"去重保守单列（fallback_ids，不是已证实的新机制）：{dict_text(fallback)}。"])
    if not check["ok"]:
        lines.append("**核对失败：** " + json.dumps({key: check[key] for key in (
            "unsettled_receipts", "amount_mismatch", "settled_without_receipt", "duplicate_receipts")}, sort_keys=True))
    return lines


def section_counts(report):
    rows, lines = [], []
    for exam_id, entry in report["exams"].items():
        counts = entry["counts"]
        if counts["status"] == "no_data":
            lines.append(f"- {exam_id}：无数据，还没有全量卷。")
            continue
        rows.extend([exam_id, row["sheet_id"], row["view"], str(row["counts"]["presented_target"]),
                     str(row["counts"]["reused"]), str(row["counts"]["mechanisms_total"]),
                     str(row["counts"]["check_bad"]), str(row["counts"]["repeat"]), str(row["masked_items"])]
                    for row in counts["sheets"])
        if counts["new_after_issue"]:
            lines.append(f"- {exam_id}：出卷后新增、还没判过的机制：{'、'.join(counts['new_after_issue'])}。")
    if not rows and not lines:
        return nothing("运行目录里没有考题")
    if not rows:
        return lines
    return [*md_table(["题", "卷子", "视图", "呈现", "复用", "机制总数", "检查样本", "重复", "被遮罩"], rows),
            "", "全量卷在出卷时都满足 呈现 + 复用 = 机制总数。比较卷是抽样，不和总数比。", *lines]


def section_calibration(report):
    if not report["calibration"]:
        return nothing("没有校准结果")
    rows = []
    for exam_id, entry in sorted(report["calibration"].items()):
        ami = entry["ami"]
        merges = {key: value["false_merges"] for key, value in (entry["difference_counts"] or {}).items()}
        splits = {key: value["false_splits"] for key, value in (entry["difference_counts"] or {}).items()}
        rows.append([exam_id, entry["status"], text_value(ami["a_human"]), text_value(ami["b_human"]), text_value(ami["a_b"]),
                     dict_text(merges) or "—", dict_text(splits) or "—"])
    return md_table(["题", "状态", "AMI A-人工", "AMI B-人工", "AMI A-B", "错并", "错拆"], rows)


def section_expansion(report):
    lines = []
    for exam_id, entry in report["exams"].items():
        hidden = entry["unjudged_count"]
        if hidden:
            lines.append(f"{exam_id}：另有 {hidden} 条机制还没判，不展开。")
        for row in entry["expansion"]:
            flags = "计分" if row["scored"] else "不计分"
            lines.extend(["", f"**{exam_id} {row['mechanism_id']}（{flags}）：{row['label']}**"])
            for member in row["members"]:
                source = f"{member['config']} 重复{member['rep']} agent{member['agent']} {member['level']} {member['model']}"
                if member["method"]:
                    source += f" {member['method']}"
                if member["returned"]:
                    source += " 退回"
                body = " ".join(f"{member['title']} {member['text']}".split())
                lines.append(f"- [{source}] {body}")
    return lines or nothing("还没有作者判断")


def stage_name(kind, value):
    return {"level": lambda: LEVEL_WORDS[value], "return": lambda: "撞车退回", "method": lambda: f"方法 {value}",
            "materials": lambda: "材料", "model": lambda: f"模型 {value}", "switch": lambda: SWITCH_WORDS[value]}[kind]()


def removal_words(comparison):
    """What a recorded leave-one comparison said, and the advice it settles. An undecided one settles nothing."""
    if comparison["decision"] == "indeterminate":
        return f"判断不了：{comparison['blockers'][0]}", None
    if comparison["decision"] == "accept":
        return "去掉后变好", REMOVABLE
    if comparison["decision"] == "prefer_simpler":
        return "去掉后没明显变化", REMOVABLE
    words = {2: "对比里有注入违规", 3: "去掉后硬约束或成本变差"}.get(comparison["matched_row"], "去掉后变差")
    return words, KEEP


def stage_rows(report):
    """One row per stage. The kept version's stages come first; a stage only the other version has follows, marked so."""
    policy = report["policy"]
    kept = report["decision"].get("preferred") or policy["baseline"]
    other = next(name for name in (policy["baseline"], policy["candidate"]) if name != kept)
    by_name = {config["name"]: config for config in report["configs"]}
    floor = exact(policy["contribution_floor"])
    rows, seen = [], set()
    for name in (kept, other):
        config = by_name[name]
        entries = [(stage, kind, value) for stage, _, kind, value, _ in stages(config)]
        entries += [(row["stage"], row["kind"], row["value"]) for row in unrecorded_switches(config)]
        for stage, kind, value in entries:
            if stage in seen:
                continue
            seen.add(stage)
            found = [row for entry in report["exams"].values()
                     for result in [entry["contribution"].get(name, {})] if result.get("status") == "ok"
                     for row in result["stages"] if row["stage"] == stage]
            numerator, denominator = sum(row["numerator"] for row in found), sum(row["denominator"] for row in found)
            waiting, present = sum(row["unjudged"] for row in found), sum(row["present"] for row in found)
            # Judged on the sum the row shows: a stage that helped on any exam is not suspect because another exam lacks it.
            # The per-exam view stays in the leave-one section of the detail.
            suspect = bool(denominator and present and not waiting and Fraction(numerator, denominator) <= floor)
            needed = "算不了：血统没记录" if kind == "switch" else f"{numerator} / {denominator}" if denominator else "无数据"
            if waiting:
                needed += f"（另有 {waiting} 条没判）"
            changes, blocked = drop_changes(config, kind, value)
            removed = None if changes is None else f"{name}__drop_{stage}"
            comparison = report["drop_comparisons"].get(removed)
            result, advice = removal_words(comparison) if comparison else (NOT_TESTED, None)
            if changes is None:
                result = f"去不掉：{blocked}"
            if advice is None:
                has_data = denominator and present
                if suspect:
                    advice = KEEP if changes is None else TRY_REMOVING
                elif has_data and Fraction(numerator, denominator) > floor:
                    advice = KEEP
                else:
                    advice = NO_DATA
            rows.append({"stage": stage, "config": name, "kind": kind, "value": value, "drop_config": removed,
                         "name": stage_name(kind, value) + ("" if name == kept else f"（`{name}` 才有）"),
                         "needed": needed, "removal_result": result, "advice": advice, "present": present,
                         "denominator": denominator, "waiting": waiting, "kept": name == kept,
                         "suspect_exams": sum(row["suspect"] for row in found), "stuck": suspect and changes is None})
    return rows


def section_stage_table(report):
    rows = report["stage_table"]
    kept = report["decision"].get("preferred") or report["policy"]["baseline"]
    return [f"按留下的版本 `{kept}` 列出。留下 = 有计分点子必须经过它；试着去掉 = 没有计分点子必须经过它，但还没跑对比；"
            "可以删 = 跑过对比，去掉后没变差；没数据 = 还没有能看的判断。",
            "", *md_table(["环节", "计分点子里有多少必须经过它", "去掉后对比结果", "建议"],
                          [[row["name"], row["needed"], row["removal_result"], row["advice"]] for row in rows])]


def conclusion_line(report):
    decision = report["decision"]
    preferred, verdict, matched = decision["preferred"], decision["decision"], decision["matched_row"]
    if verdict == "accept":
        return f"结论：这次改动有用，留 `{preferred}`。多数判定题的分数提升超过了重复之间的波动。"
    if verdict == "reject":
        why = {2: "出现了注入违规", 3: decision["reason"]}.get(matched, "多数判定题的分数下降超过了重复之间的波动")
        return f"结论：这次改动不能留，留 `{preferred}`。原因：{why}。"
    if verdict == "prefer_simpler":
        if preferred is None:
            return "结论：没看出这次改动明显变好或变坏，但配置里没写复杂度，选不出该留哪个。"
        return f"结论：没看出这次改动明显变好或变坏，留更简单的 `{preferred}`。"
    blockers = decision["blockers"]
    more = f"（还有 {len(blockers) - 1} 条，见明细第 1 节）" if len(blockers) > 1 else ""
    return f"结论：还判断不了这次改动有没有用，先别换版本。原因：{blockers[0]}{more}。"


def next_step_line(report):
    rows = report["stage_table"]
    # Order: stages already shown removable come first because no further run is needed; then the kept version's,
    # since that is what gets edited; then stages suspect on more exams; then more ideas spent, as removing those
    # saves the most. The stage id breaks ties so the order never changes between runs.
    order = lambda row: (row["advice"] != REMOVABLE, not row["kept"], -row["suspect_exams"], -row["present"], row["stage"])
    actionable = sorted((row for row in rows if row["advice"] in (REMOVABLE, TRY_REMOVING)), key=order)
    picked, parts = actionable[:3], []
    for row in picked:
        plain = stage_name(row["kind"], row["value"])
        parts.append(f"{plain}可以删（配置 `{row['drop_config']}`，对比已跑过）" if row["advice"] == REMOVABLE
                     else f"试着去掉{plain}（用配置 `{row['drop_config']}` 跑对比）")
    if picked:
        more = f"，还有 {len(actionable) - 3} 个见环节表" if len(actionable) > 3 else ""
        note = "这只是相关，要跑对比才算数。" if any(row["advice"] == TRY_REMOVING for row in picked) else ""
        return f"下一步：{'；'.join(parts)}{more}。完整配置在 report.json。{note}"
    stuck = [row for row in rows if row["stuck"]]
    if stuck:
        return f"下一步：只有{stage_name(stuck[0]['kind'], stuck[0]['value'])}可疑，但它去不掉，没有能跑的对比。"
    if not any(row["denominator"] for row in rows):
        return "下一步：还没有作者判断或计分的点子，没有数据，说不出该去掉哪个环节。"
    waiting = "还有环节没判完，判完再看。" if any(row["waiting"] for row in rows) else ""
    return f"下一步：现在没有可疑环节，不用去掉什么。{waiting}"


def trust_line(report):
    notes = []
    if "fake" in report["costs"]["backends"]:
        notes.append("这次用假后端，判定只证明流程接通了，不是真结论。")
    sizes = [entry["per_unit"] for entry in report["exams"].values() if entry.get("per_unit") is not None]
    if sizes and min(sizes) < MIN_UNIT_SAMPLE:
        notes.append(f"样本小：每个单元最少只有 {min(sizes)} 条，只能看方向。")
    if report["unrevealed_sheets"]:
        notes.append(f"还有 {report['unrevealed_sheets']} 张卷子没还原，结论可能变。")
    if report["self_check"]["warning"]:
        notes.append(f"规则自检有警告：{report['self_check']['warning']}。")
    unfinished = sum(result["completion"]["status"] != "ok" for entry in report["exams"].values()
                     for reps in entry["units"].values() for result in reps.values())
    if unfinished:
        notes.append(f"有 {unfinished} 个单元失败或没跑完，判定不完整。")
    if not report["receipts"]["ok"]:
        notes.append("收据和账本对不上，费用不可信。")
    return "能信多少：" + ("".join(notes) or "没发现影响结论的问题。")


def replacement_sentence(row):
    head = f"题 {row['exam_id']} 配置 `{row['config']}` 的第 {row['failed_rep']} 次重复失败，换成第 {row['new_rep']} 次"
    money_text = f"失败那次花了 {money(row['failed_usd'])} 美元，替换那次花了 {money(row['new_usd'])} 美元"
    status = {"complete": "", "failed": "；替换也失败了，不能再替换，要人来决定", "unfinished": "；替换还没跑完"}[row["status"]]
    return f"{head}；{money_text}{status}。"


def summary_lines(report):
    """Five lines at most, for someone who has never seen the code. Config names stay: they are what to run."""
    lines = [conclusion_line(report), next_step_line(report), trust_line(report)]
    if report["replacements"]:
        lines.append("替换：" + "".join(replacement_sentence(row) for row in report["replacements"]))
    return lines


def section_replacements(report):
    if not report["replacements"]:
        return ["没有单元被替换。"]
    lines = ["失败单元的文件和费用都保留，账本不删不改。它的费用只算在总账里，不进判定的成本比较。"]
    for row in report["replacements"]:
        cause = STAGE_WORDS[row["stage"]]
        lines.append(f"- {replacement_sentence(row)}失败环节：{cause}。")
        if row["ungrouped_ideas"]:
            lines.append(f"  - 失败单元有 {row['ungrouped_ideas']} 条点子没分组，留在磁盘上，不进卷子。")
        if row["reason"]:
            lines.append(f"  - 作者写的原因：{row['reason']}")
    return lines


SECTIONS = (("判定", section_decision), ("主分和噪声线", section_scores), ("护栏和成本", section_guards),
            ("贡献度", section_contribution), ("留一对照", section_leave_one), ("失败账本", section_failures),
            ("计分规则自检", section_self_check), ("想试比例和计了分但不想试", section_want),
            ("机器指标和两种比较口径", section_metrics), ("费用", section_costs), ("条数核对", section_counts),
            ("校准", section_calibration), ("被合并的说法", section_expansion), ("单元替换", section_replacements))


def labelled(line):
    """Bold the label a summary line opens with."""
    label, rest = line.split("：", 1)
    return f"**{label}：**{rest}"


def report_parts(report):
    return ("头脑风暴评测报告", [labelled(line) for line in report["summary"]], section_stage_table(report),
            [(f"{number}. {title}", render(report)) for number, (title, render) in enumerate(SECTIONS, 1)])


def fold_markdown(title, body):
    """Stored text cannot open or close a fold, or reach a Markdown viewer as live HTML."""
    text = "\n".join(body).replace("<", "&lt;")
    return f"<details>\n<summary>{title}</summary>\n\n{text}\n\n</details>\n"


def render_markdown(report):
    title, summary, table, details = report_parts(report)
    lines = [f"# {title}", "", "## 先看这里", "", *(f"- {line}" for line in summary), "", "## 环节表", "", *table,
             "", f"## {DETAIL_TITLE}", ""]
    return "\n".join(lines) + "\n" + "\n".join(fold_markdown(*part) for part in details)


ADVICE_CLASSES = {KEEP: "keep", TRY_REMOVING: "try", REMOVABLE: "remove", NO_DATA: "none"}
PAGE_STYLE = """
:root { color-scheme: light dark; --bg: #faf9f6; --panel: #ffffff; --ink: #1f2328; --muted: #59616b; --line: #d8d6cf;
  --code: #efede7; --keep: #1c6b3c; --keep-bg: #e0f1e6; --try: #875400; --try-bg: #fbeed2;
  --remove: #a12d24; --remove-bg: #fae3e0; --none: #59616b; --none-bg: #ececea; }
@media (prefers-color-scheme: dark) { :root { --bg: #16181b; --panel: #1f2226; --ink: #e8e6e1; --muted: #a4abb3; --line: #3a3f45;
  --code: #2c3036; --keep: #8fd9aa; --keep-bg: #1b3526; --try: #f0c674; --try-bg: #3b2f14;
  --remove: #f3a79f; --remove-bg: #421f1b; --none: #b4bac1; --none-bg: #2c3036; } }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 62rem; padding: 2rem 1rem 4rem; background: var(--bg); color: var(--ink);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", "Segoe UI", sans-serif; }
h1 { font-size: 1.5rem; margin: 0 0 1.25rem; }
h2 { font-size: 1.15rem; margin: 2.25rem 0 .75rem; }
p { margin: .5rem 0; overflow-wrap: anywhere; }
code { background: var(--code); border-radius: 4px; padding: .05em .35em; font: .9em ui-monospace, Menlo, Consolas, monospace; overflow-wrap: anywhere; }
ul { margin: .5rem 0; padding-left: 1.25rem; }
li { margin: .3rem 0; overflow-wrap: anywhere; }
li.sub { margin-left: 1.25rem; color: var(--muted); }
.first { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: .5rem 1.25rem; }
.first h2 { margin-top: .75rem; }
.first ul { list-style: none; padding: 0; }
.first li { margin: .75rem 0; }
.scroll { overflow-x: auto; margin: .75rem 0; }
table { border-collapse: collapse; width: 100%; background: var(--panel); font-size: .95em; }
th, td { border: 1px solid var(--line); padding: .45rem .65rem; text-align: left; vertical-align: top; }
th { color: var(--muted); font-weight: 600; white-space: nowrap; }
.tag { display: inline-block; border-radius: 4px; padding: 0 .5em; font-weight: 600; white-space: nowrap; }
.tag.keep { color: var(--keep); background: var(--keep-bg); }
.tag.try { color: var(--try); background: var(--try-bg); }
.tag.remove { color: var(--remove); background: var(--remove-bg); }
.tag.none { color: var(--none); background: var(--none-bg); }
details { border-top: 1px solid var(--line); padding: .55rem 0; }
details:last-of-type { border-bottom: 1px solid var(--line); }
summary { cursor: pointer; font-weight: 600; }
details > :not(summary) { margin-left: 1rem; }
"""


def inline_html(text):
    """Escape first, so stored text can only ever become text. The two marks added afterwards hold no attributes."""
    parts = re.split(r"(`[^`]+`)", html.escape(text, quote=True))
    return "".join(f"<code>{part[1:-1]}</code>" if part.startswith("`") and len(part) > 2 and part.endswith("`")
                   else re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", part) for part in parts)


def table_html(rows):
    """Rows as md_table wrote them, where a pipe inside a cell is backslash-escaped."""
    split = lambda line: [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip()[1:-1])]
    head = "".join(f"<th>{inline_html(cell)}</th>" for cell in split(rows[0]))
    def cell(value):
        kind = ADVICE_CLASSES.get(value)
        return f'<td><span class="tag {kind}">{inline_html(value)}</span></td>' if kind else f"<td>{inline_html(value)}</td>"
    body = "".join("<tr>" + "".join(map(cell, split(row))) + "</tr>\n" for row in rows[2:])
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead>\n<tbody>\n{body}</tbody></table></div>'


def blocks_html(lines):
    output, index = [], 0
    lines = "\n".join(lines).split("\n")
    while index < len(lines):
        line = lines[index]
        if line.startswith("|"):
            end = index
            while end < len(lines) and lines[end].startswith("|"):
                end += 1
            output.append(table_html(lines[index:end]))
            index = end
        elif line.startswith(("- ", "  - ")):
            items = []
            while index < len(lines) and lines[index].startswith(("- ", "  - ")):
                nested = lines[index].startswith("  - ")
                items.append(f"<li{' class=\"sub\"' if nested else ''}>{inline_html(lines[index].lstrip()[2:])}</li>")
                index += 1
            output.append("<ul>" + "".join(items) + "</ul>")
        else:
            if line.strip():
                output.append(f"<p>{inline_html(line)}</p>")
            index += 1
    return "\n".join(output)


def render_html(title, summary, table, details):
    folded = "\n".join(f"<details>\n<summary>{inline_html(name)}</summary>\n{blocks_html(body)}\n</details>" for name, body in details)
    bullets = "".join(f"<li>{inline_html(line)}</li>" for line in summary)
    # The policy backs up the escaping: even a slip here cannot load or run anything.
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{inline_html(title)}</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
<h1>{inline_html(title)}</h1>
<section class="first">
<h2>先看这里</h2>
<ul>{bullets}</ul>
</section>
<section>
<h2>环节表</h2>
{blocks_html(table)}
</section>
<section>
<h2>{DETAIL_TITLE}</h2>
{folded}
</section>
</body>
</html>
"""


def render_report_html(report):
    return render_html(*report_parts(report))


def removal_pairs(configs):
    """Leave-one configs among the supplied ones, recognised by the name the report told the author to use."""
    names = {config["name"] for config in configs}
    return [(config["name"], f"{config['name']}__drop_{stage}") for config in configs
            for stage in [entry[0] for entry in stages(config)] + [row["stage"] for row in unrecorded_switches(config)]
            if f"{config['name']}__drop_{stage}" in names]


def removal_decisions(removals, tested, decision_exam_ids, policy):
    """The same acceptance table per removal. Removing a stage is simpler by construction, whatever complexity was copied."""
    output = {}
    for parent, removed in removals:
        result = decide([entry[removed] for entry in tested.values()], decision_exam_ids=decision_exam_ids,
                        policy={"baseline": parent, "candidate": removed, "cost_ratio": policy["cost_ratio"]},
                        complexity={parent: 1, removed: 0})
        output[removed] = {key: result[key] for key in ("decision", "preferred", "matched_row", "reason", "blockers")}
    return output


def build_report(run_dir, exams, configs, *, baseline, candidate, decision_exam_ids, seed=0, ledger_path=None,
                 cost_ratio=DEFAULT_COST_RATIO, check_bad_ratio=DEFAULT_CHECK_BAD_RATIO,
                 contribution_floor=DEFAULT_CONTRIBUTION_FLOOR, exact_receipts=False):
    root = Path(run_dir).resolve()
    names = [config["name"] for config in configs]
    if baseline == candidate or baseline not in names or candidate not in names:
        raise ValueError("Report requires two distinct configs among those supplied")
    # Refuse a malformed threshold before any work, not after every unit was recomputed.
    policy = {"baseline": baseline, "candidate": candidate, "cost_ratio": amount(cost_ratio),
              "check_bad_ratio": amount(check_bad_ratio), "contribution_floor": amount(contribution_floor)}
    selected = [exam for exam in exams if (root / exam["id"]).is_dir()]
    removals = removal_pairs(configs)
    per_exam = {exam["id"]: exam_report(root, exam, configs, baseline=baseline, candidate=candidate, seed=seed,
                                        floor=contribution_floor, removals=removals) for exam in selected}
    tested = {entry_id: entry.pop("removal_evidence") for entry_id, entry in per_exam.items()}
    decision = decide([entry["evidence"] for entry in per_exam.values()], decision_exam_ids=decision_exam_ids,
                      policy=policy, complexity={config["name"]: config.get("complexity") for config in configs})
    receipts = load_receipts(root)
    costs = cost_summary(receipts)
    if ledger_path is None:
        # ceiling: one ledger per report. A run mixing fake and real calls checks against the real one and reports the rest.
        ledger_path = default_ledger_path("fake" if set(costs["backends"]) <= {"fake"} else "claude", root)
    events = read_ledger(ledger_path)
    check = reconcile(receipts, events, exact_match=exact_receipts)
    contributions = {exam_id: entry["contribution"] for exam_id, entry in per_exam.items()}
    calibration = calibration_rows(root, set(per_exam))
    report = {"schema_version": 1, "decision": decision, "policy": policy, "exams": per_exam, "configs": configs,
              "contribution_note": CONTRIBUTION_NOTE, "sample_note": SAMPLE_NOTE,
              "leave_one": leave_one_rows(configs, contributions),
              "failures": [*(row for entry in per_exam.values() for row in entry["failures"]), *manual_rows(calibration)],
              "self_check": rule_self_check(self_check_results(root), check_bad_ratio),
              "costs": costs, "receipts": check, "calibration": calibration, "replay": replay_rows(root, set(per_exam)),
              "unrevealed_sheets": sum(not is_revealed(path) for path, _ in sheet_keys(root)),
              "warnings": [f"{exam_id}：{text}" for exam_id, entry in per_exam.items() for text in entry["warnings"]],
              "drop_comparisons": removal_decisions(removals, tested, decision_exam_ids, policy),
              "replacements": [describe_replacement(root, row, events) for exam_id in per_exam
                               for row in replacements.load(root, exam_id)]}
    report["warnings"].extend(f"{exam_id}：有机制不在全量卷里，还没判：{'、'.join(entry['counts']['new_after_issue'])}"
                              for exam_id, entry in per_exam.items() if entry["counts"]["new_after_issue"])
    if report["self_check"]["warning"]:
        report["warnings"].append(report["self_check"]["warning"])
    if not check["ok"]:
        report["warnings"].append("收据和账本对不上，见第 10 节")
    report["stage_table"] = stage_rows(report)
    report["summary"] = summary_lines(report)
    atomic_write(root / "report.json", metric_dumps(json_ready(report)) + "\n")
    atomic_write(root / "report.md", render_markdown(report))
    atomic_write(root / "report.html", render_report_html(report))
    return report


def run_cli(args):
    if len(args.config) < 2:
        raise ValueError("Report requires at least two --config files")
    if args.run_dir.resolve().is_relative_to(args.exam_dir.resolve()):
        raise ValueError("run-dir must not be inside the read-only exam-dir")
    configs = [load_config(path) for path in args.config]
    names = [config["name"] for config in configs]
    exams = load_exams(args.exam_dir)
    if args.judge_config:
        judging = load_config(args.judge_config)
        backend = stage_backend(judging, fake_judge)
        ledger = BudgetLedger(args.ledger or default_ledger_path(backend.name, args.run_dir),
                              args.limit if args.limit is not None else judging["budget_usd"])
        for exam in exams:
            if not exam["constraints"]:
                continue
            for name in names:
                for rep in unit_reps(args.run_dir, exam["id"], name):
                    try:
                        judge_unit(args.run_dir, exam, name, rep, backend=backend, ledger=ledger,
                                   model=judging["default_model"], max_output_tokens=judging["max_output_tokens"])
                    except NotStarted as exc:
                        raise ValueError(f"Constraint check stopped before provider startup: {exc}") from exc
    report = build_report(args.run_dir, exams, configs, baseline=args.baseline, candidate=args.candidate,
                          decision_exam_ids=[key for key in args.decision_exams.split(",") if key], seed=args.seed,
                          ledger_path=args.ledger, cost_ratio=args.cost_ratio, check_bad_ratio=args.check_bad_ratio,
                          contribution_floor=args.contribution_floor)
    print(metric_dumps({key: report["decision"][key] for key in ("decision", "preferred", "matched_row", "reason")}))


def register(subparsers):
    parser = subparsers.add_parser("report", help="Decision, diagnostics and report; read-only without --judge-config")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--exam-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, action="append", required=True, help="Repeat for every compared config")
    parser.add_argument("--baseline", required=True, help="Config name of the accepted version")
    parser.add_argument("--candidate", required=True, help="Config name of the changed version")
    parser.add_argument("--decision-exams", required=True, help="Comma-separated exam ids; there is no default")
    parser.add_argument("--seed", type=int, default=0, help="Seed of the two comparison controls")
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--judge-config", type=Path, help="Pay for hard-constraint checks through the ledger")
    parser.add_argument("--limit")
    parser.add_argument("--cost-ratio", default=DEFAULT_COST_RATIO, help="Provisional default")
    parser.add_argument("--check-bad-ratio", default=DEFAULT_CHECK_BAD_RATIO, help="Provisional default")
    parser.add_argument("--contribution-floor", default=DEFAULT_CONTRIBUTION_FLOOR, help="Provisional default")
    parser.set_defaults(func=lambda args: cli_boundary(run_cli, args))
