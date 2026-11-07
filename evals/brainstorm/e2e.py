"""Fake-backend wiring run: loop, grouping, calibration, constraints, sheets, simulated answers, report.

There is deliberately no real-backend entry here: a full run would be a paid exam, which needs its own decision.
"""

from functools import partial
import json
from pathlib import Path
import random
import re
import sys

from . import dedupe, judge, record, replay, report, sheet
from .backends import FakeBackend
from .budget import BudgetLedger, atomic_write, cli_boundary, default_ledger_path, dumps
from .config import DEFAULTS, validate_config
from .exam import load_exams
from .loop import run_loop


EXAM_DIR = Path(__file__).resolve().parent / "exams" / "samples"
DEFAULT_EXAMS = ("open_sample", "injection_sample", "parts_sample")
DEFAULT_SEED = 20261103
REPS = 3
METHODS = {"angle_north": "Test placeholder: start from who uses it.",
           "angle_south": "Test placeholder: start from what breaks first.",
           "angle_east": "Test placeholder: start from what can be removed."}
GROUP_BUCKETS = 11
# Agents per config are 2, so one size can be sampled and one cannot: replay must report both honestly.
REPLAY_SIZES = (2, 5)
TITLE_VARIANTS = 89
POOL_WORDS = {"base": "常规", "first": "换位", "second": "组合", "back": "替换"}
POOL_SIZES = {"base": 4, "first": 8, "second": 8, "back": 6}


def body(pool, number):
    return f"{POOL_WORDS[pool]}第{number}种安排：把这件事交给固定的人轮流负责，每周核对一次，出问题就退回上一步。"


def sized_hash(*parts):
    return int(record.digest(list(parts)), 16)


def data_block(prompt, header):
    """The data block that follows a trusted header line, per the wrap_data layout."""
    lines = prompt.split("\n")
    start = lines.index(header) + 2
    return "\n".join(lines[start + 1:lines.index(lines[start], start + 1)])


def idea(pool, number, salt, cards, position):
    # Different prompts give different titles, so one mechanism collects several wordings.
    return {"title": f"{POOL_WORDS[pool]}说法{sized_hash(salt, pool, number) % TITLE_VARIANTS}", "text": body(pool, number),
            # Every third idea declares no card, so the failure ledger has something real to find.
            "cards_used": cards if position % 3 != 2 else [], "decomposition": None}


def scenario(prompt, model, max_output_tokens, *, marker=None):
    """Deterministic in the prompt alone, so a resumed run gets the same answers. Text names no source term."""
    level = re.search(r"^Generation level: (\w+)\.$", prompt, re.MULTILINE).group(1)
    cards = [row["id"] for row in json.loads(data_block(prompt, "Assigned cards:"))]
    rng = random.Random(prompt)
    if "Collision return: replace" in prompt:
        originals = json.loads(data_block(prompt, "Return exactly one replacement idea for each original, in the same order."))
        ideas = []
        for position, original in enumerate(originals):
            salt = prompt + str(position)
            # Half of the early rewrites only change wording, and the last level always does.
            if level == "pressure1" or sized_hash(salt) % 2 == 0:
                ideas.append({"title": "换个说法" + str(sized_hash(salt) % TITLE_VARIANTS), "text": original["text"],
                              "cards_used": cards[:1], "decomposition": None, "replaces": position + 1})
            else:
                ideas.append({**idea("back", rng.randrange(POOL_SIZES["back"]), salt, cards[:1], 0), "replaces": position + 1})
        return json.dumps({"ideas": ideas}, ensure_ascii=False)
    if level == "baseline":
        # The extra one is never picked later, so every run keeps a mechanism only the full view can show.
        picks = [("base", number) for number in range(POOL_SIZES["base"] + 1)]
    elif level == "round1":
        picks = [("base", rng.randrange(POOL_SIZES["base"])),
                 *(("first", number) for number in rng.sample(range(POOL_SIZES["first"]), 3))]
    elif level == "round2":
        picks = [("first", rng.randrange(POOL_SIZES["first"])),
                 *(("second", number) for number in rng.sample(range(POOL_SIZES["second"]), 3))]
    else:
        # This level only repeats what the baseline holds, which is what running dry looks like.
        picks = [("base", number) for number in rng.sample(range(POOL_SIZES["base"]), 2)]
    ideas = [idea(pool, number, prompt, cards, position) for position, (pool, number) in enumerate(picks)]
    if marker is not None and level != "baseline":
        ideas[0]["text"] += " " + marker
    return json.dumps({"ideas": ideas}, ensure_ascii=False)


def simulated_answer(seed, text):
    """Hash of seed and visible text only: the same input any blind reader has."""
    value = sized_hash(seed, text)
    return {"unthought": value % 4 != 0, "care": (value >> 8) % 4 != 0,
            "fatal_reason": "模拟答卷：说得出致命原因" if (value >> 16) % 5 == 0 else None,
            "want_to_try": bool((value >> 24) % 2)}


def simulate_answers(run_dir, sheet_id, seed):
    """Fill a sheet through the same public state the page reads. It never opens the key."""
    state = sheet.public_state(run_dir, sheet_id)
    if state["kind"] == "group":
        buckets = {}
        for item in state["items"]:
            bucket = sized_hash(seed, item["text"].split("\n", 1)[-1]) % GROUP_BUCKETS
            buckets.setdefault(bucket, []).append(item["item_id"])
        payload = {"sheet_id": sheet_id, "groups": [buckets[key] for key in sorted(buckets)]}
    else:
        payload = {"sheet_id": sheet_id, "answers": [{"item_id": item["item_id"], **simulated_answer(seed, item["text"])}
                                                     for item in state["items"]]}
    if state["items"]:
        sheet.merge_answers(run_dir, sheet_id, payload)


def complete_sheet(root, exam, sheet_id, seed, **options):
    """Every step is skipped once done: reissuing and answering a revealed sheet are both errors."""
    path = root / "sheets" / sheet_id
    if not (path / "sheet.json").exists():
        sheet.make_sheet(root, exam, sheet_id=sheet_id, seed=seed, **options)
    if not (path / "revealed.json").exists():
        simulate_answers(root, sheet_id, seed)
    return sheet.reveal(root, sheet_id)


def write_inputs(root):
    shared = {"agents": 2, "default_model": "gen_alpha", "levels": ["round1", "round2", "pressure1"],
              "method_mode": "different", "methods": METHODS, "backend": {"kind": "fake"}}
    raw = [{"name": "cfg_plain", "complexity": 1, **shared},
           {"name": "cfg_return", "complexity": 2, "return_collisions": True, **shared},
           {"name": "cfg_classifier", "default_model": "clf_alpha", "models": ["clf_alpha", "clf_beta"],
            "backend": {"kind": "fake"}}]
    for row in raw:
        atomic_write(root / "e2e_inputs" / f"{row['name']}.json", dumps(row) + "\n")
    return [validate_config(row) for row in raw]


def simulated_subproblems(root, exam_id):
    """Written by this script and labelled so: a wiring fixture for one ledger row, not real annotation."""
    ideas = [row for row in sheet.load_ideas(root, exam_id).values() if row["level"] != "baseline"]
    targets = ["取用", "归还", "没人想到的部分"]
    atomic_write(root / exam_id / "subproblems.json", dumps({
        "exam_id": exam_id, "simulated": True, "targets": targets,
        "matches": {row["idea_id"]: [targets[sized_hash(row["text"]) % 2]] for row in ideas}}) + "\n")


def progress(exam_id, stage):
    print(f"E2E {exam_id} stage={stage}", file=sys.stderr)


def run_exam(root, exam, configs, classifier, *, seed, reps, ledger, marker):
    exam_id = exam["id"]
    generation = FakeBackend(responses=partial(scenario, marker=marker))
    grouping = FakeBackend(responses=dedupe.fake_partition)
    for config in configs:
        for rep in range(1, reps + 1):
            run_loop(exam, config, root, seed=seed, backend=generation, ledger=ledger, dedupe_config=classifier,
                     dedupe_backend=grouping, rep=rep)
    # A finished loop rewrites its state once on the next open. Replay binds to the hashes of those files, so settle
    # them now: a resumed run reopens every loop, and that must not look like changed sources.
    for config in configs:
        for rep in range(1, reps + 1):
            run_loop(exam, config, root, seed=seed, backend=generation, ledger=ledger, dedupe_config=classifier,
                     dedupe_backend=grouping, rep=rep)
    progress(exam_id, "group_sheet")
    complete_sheet(root, exam, f"{exam_id}_group", seed, kind="group")
    progress(exam_id, "calibration")
    models = classifier["models"]
    dedupe.calibrate(record.read_rows(root / exam_id / "human_group_ideas.jsonl"),
                     record.read_file(root / exam_id / "human_groups.json"), root, exam_id=exam_id, seed=seed,
                     backend=grouping, ledger=ledger, model_a=models[0], model_b=models[1], allowed_models=models,
                     name=f"calibrate_{exam_id}")
    progress(exam_id, "constraint")
    judging = FakeBackend(responses=judge.fake_judge)
    for config in configs:
        for rep in range(1, reps + 1):
            report.judge_unit(root, exam, config["name"], rep, backend=judging, ledger=ledger, model=models[0])
    progress(exam_id, "replay")
    replay.run_replay(root, exam, config=configs[0]["name"], rep=1, name=f"sampled_{exam_id}", seed=seed,
                      dedupe_config=classifier, judge_config=classifier, ledger=ledger, sizes=REPLAY_SIZES,
                      dedupe_backend=grouping, judge_backend=judging)
    progress(exam_id, "compare_sheet")
    complete_sheet(root, exam, f"{exam_id}_compare", seed, kind="score", configs=[config["name"] for config in configs],
                   backend=FakeBackend(responses=sheet.fake_rewrite), ledger=ledger, model=models[0])
    progress(exam_id, "full_sheet")
    result = complete_sheet(root, exam, f"{exam_id}_full", seed, kind="score", all_view=True)
    if result.get("unjudged_mechanisms"):
        raise ValueError(f"Conservation failed: unjudged mechanisms remain: {result['unjudged_mechanisms']}")
    if exam["type"] == "decomposable":
        simulated_subproblems(root, exam_id)


def assignment_digest(root, exams, configs, reps):
    allocations = {exam["id"]: {config["name"]: {str(rep): record.read_file(
        record.output_dir(root, exam["id"], config["name"], rep) / "run_summary.json")["allocations"]
        for rep in range(1, reps + 1)} for config in configs} for exam in exams}
    return record.digest(allocations)


def run_e2e(run_dir, *, seed=DEFAULT_SEED, budget_usd=DEFAULTS["budget_usd"], exam_ids=DEFAULT_EXAMS, reps=REPS, marker=None):
    root = Path(run_dir).resolve()
    if root.is_relative_to(EXAM_DIR.resolve()):
        raise ValueError("run-dir must not be inside the read-only exam-dir")
    root.mkdir(parents=True, exist_ok=True)
    by_id = {exam["id"]: exam for exam in load_exams(EXAM_DIR)}
    exams = [by_id[exam_id] for exam_id in exam_ids]
    *configs, classifier = write_inputs(root)
    ledger = BudgetLedger(default_ledger_path("fake", root), budget_usd)
    for exam in exams:
        run_exam(root, exam, configs, classifier, seed=seed, reps=reps, ledger=ledger, marker=marker)
    built = report.build_report(root, exams, configs, baseline=configs[0]["name"], candidate=configs[1]["name"],
                                decision_exam_ids=list(exam_ids), seed=seed, ledger_path=ledger.path, exact_receipts=True)
    late = {exam_id: entry["counts"]["new_after_issue"] for exam_id, entry in built["exams"].items() if entry["counts"]["new_after_issue"]}
    if late:
        raise ValueError(f"Conservation failed: mechanisms missing from the full view: {late}")
    if not built["receipts"]["ok"]:
        raise ValueError("Receipts and the fake ledger disagree; see report.json")
    if built["receipts"]["open_events"]:
        raise ValueError("The fake ledger still holds unsettled reservations or uncertain calls")
    per_exam = {exam_id: entry["counts"] for exam_id, entry in built["exams"].items()}
    totals = {field: sum(entry[field] for entry in per_exam.values())
              for field in ("mechanisms_total", "presented_target", "reused")}
    summary = {"calls_total": built["receipts"]["settled"], "usd_total": built["receipts"]["settled_usd"], **totals,
               "assignment_digest": assignment_digest(root, exams, configs, reps),
               "decision": built["decision"]["decision"], "preferred": built["decision"]["preferred"],
               "exams": {exam_id: {**{field: entry[field] for field in totals},
                                   "masked_items": sum(row["masked_items"] for row in entry["sheets"])}
                         for exam_id, entry in per_exam.items()}}
    atomic_write(root / "e2e_summary.json", dumps(summary) + "\n")
    return summary


def run_cli(args):
    print(dumps(run_e2e(args.run_dir, seed=args.seed, budget_usd=args.budget_usd)))


def register(subparsers):
    parser = subparsers.add_parser("e2e", help="Wiring run on the fake backend: its decision is not a finding")
    parser.add_argument("--fake", action="store_true", required=True, help="Required: there is no real-backend mode")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--budget-usd", default=DEFAULTS["budget_usd"])
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.set_defaults(func=lambda args: cli_boundary(run_cli, args))
