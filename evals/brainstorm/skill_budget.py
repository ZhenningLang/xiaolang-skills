"""Measure the brainstorm skill against plain repeated asking at the same number of sub-agent calls.

Standard library only; it drives skills/brainstorm/scripts/brainstorm.py and never calls a model itself.
The host dispatches the sub-agent tasks, exactly as it does for the skill.

  direct-init  start a control run: N batches of 3 agents, no cards, no method, no rounds
  next         advance a control run (also used after `merge`)
  merge        add every idea of a finished skill run to the control run and group them by the same grouper
  compare      print how many mechanisms each side reached and how many only one side reached
  check        machine-checkable pass/fail for a finished skill run (the pre-release smoke check)
  audit-init   write a blind prompt for a judge: repeats under one fixed rule, and hard-constraint violations
  audit-read   read the judge's answer
  repeat       compare two or more groupings of the same merged skill run: how far the counts move
"""

import argparse
import importlib.util
import itertools
import json
from pathlib import Path
import random
import sys

SKILL_SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "brainstorm" / "scripts" / "brainstorm.py"
IMPORT_PREFIX = "skill:"
IMPORT_BATCH_OFFSET = 1000


def load_skill():
    spec = importlib.util.spec_from_file_location("brainstorm_skill_script", SKILL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PROFILES["direct"] = []
    return module


bs = load_skill()


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=1))


def cmd_direct_init(args):
    if args.batches < 1:
        raise bs.RunError("--batches must be positive")
    argv = ["init", "--dir", args.dir, "--question-file", args.question_file, "--profile", "direct",
            "--ideas-per-agent", str(args.ideas_per_agent)]
    for item in args.constraint or []:
        argv += ["--constraint", item]
    if args.seed is not None:
        argv += ["--seed", str(args.seed)]
    if bs.main(argv) != 0:
        raise bs.RunError("init failed")
    state = bs.load_state(args.dir)
    state["direct_batches"] = args.batches
    bs.save_state(args.dir, state)


def cmd_next(args):
    state = bs.load_state(args.dir)
    if "direct_batches" not in state:
        raise bs.RunError("not a control run; start one with direct-init")
    if "merge" not in state:
        bs.BASELINE_BATCHES = state["direct_batches"]
        code = bs.main(["next", "--dir", args.dir] + (["--give-up"] if args.give_up else []))
        if code != 0:
            raise bs.RunError("next failed")
        return
    if state["merge"]["done"]:
        emit({"action": "done", **comparison(state)})
        return
    missing = [key for key in state["pending"] if not Path(state["tasks"][key]["output"]).is_file()]
    if missing and not args.give_up:
        emit({"action": "wait", "missing": [
            {"id": key, "prompt": state["tasks"][key]["prompt"], "output": state["tasks"][key]["output"]}
            for key in missing], "instructions": bs.WAIT_TEXT})
        return
    bs.ingest_pending(state)
    bs.check_partition(state)
    if state["queue"]:
        result = bs.dispatch(args.dir, state, bs.issue_grouping(args.dir, state), "group")
    else:
        state["merge"]["done"] = True
        result = {"action": "done", **comparison(state)}
    bs.save_state(args.dir, state)
    emit(result)


def cmd_merge(args):
    state = bs.load_state(args.dir)
    if "direct_batches" not in state or not state["complete"]:
        raise bs.RunError("finish the control run first")
    if "merge" in state:
        raise bs.RunError("a skill run was already merged into this control run")
    source = bs.load_state(args.source)
    if not source["complete"]:
        raise bs.RunError("the skill run is not finished")
    if source["question"] != state["question"]:
        raise bs.RunError("the two runs answer different questions")
    order = bs.idea_order(source)
    ids = []
    for row in sorted(source["ideas"].values(), key=lambda row: order[row["idea_id"]]):
        key = "x-" + row["idea_id"]
        state["ideas"][key] = {**row, "idea_id": key, "level": IMPORT_PREFIX + row["level"],
                               "batch": IMPORT_BATCH_OFFSET + row["batch"], "task": "import", "returned_from": None}
        ids.append(key)
    bs.enqueue(state, ids)
    state["merge"] = {"done": False, "source": str(Path(args.source).resolve()), "source_summary": {
        "ideas": len(source["ideas"]), "mechanisms": len(source["mechanisms"]),
        "beyond_its_own_baseline": sum(not row["in_baseline"] for row in source["mechanisms"]),
        "idea_calls": sum(1 for task in source["tasks"].values() if task["kind"] != "group"),
        "idea_calls_dropped": sum(1 for task in source["tasks"].values()
                                  if task["kind"] != "group" and task["status"] == "failed")}}
    bs.save_state(args.dir, state)
    emit({"merged_ideas": len(ids), "grouping_chunks": len(state["queue"]), "next": "run next and dispatch as usual; group with the strongest model you have"})


def comparison(state):
    ideas = state["ideas"]
    control = [row for row in ideas.values() if row["level"] == "baseline"]
    batches = sorted({row["batch"] for row in control})
    first_batch = {}
    sides = {"control_only": 0, "skill_only": 0, "both": 0}
    skill_only_by_level, landed_in_control = {}, {}
    order = bs.idea_order(state)
    for mechanism in state["mechanisms"]:
        members = sorted(mechanism["idea_ids"], key=order.get)
        mine = [key for key in members if ideas[key]["level"] == "baseline"]
        theirs = [key for key in members if ideas[key]["level"].startswith(IMPORT_PREFIX)]
        if mine:
            first_batch[mechanism["mechanism_id"]] = ideas[mine[0]]["batch"]
        sides["both" if mine and theirs else "control_only" if mine else "skill_only"] += 1
        if theirs and not mine:
            level = ideas[theirs[0]]["level"][len(IMPORT_PREFIX):]
            skill_only_by_level[level] = skill_only_by_level.get(level, 0) + 1
        if mine:
            for key in theirs:
                level = ideas[key]["level"][len(IMPORT_PREFIX):]
                landed_in_control[level] = landed_in_control.get(level, 0) + 1
    curve, seen = [], 0
    for batch in batches:
        new = sum(1 for value in first_batch.values() if value == batch)
        seen += new
        curve.append({"batch": batch, "calls_so_far": bs.AGENTS * (batches.index(batch) + 1),
                      "new_mechanisms": new, "mechanisms_so_far": seen})
    result = {"control": {"idea_calls": sum(1 for task in state["tasks"].values() if task["kind"] == "generate"),
                          "idea_calls_dropped": sum(1 for task in state["tasks"].values()
                                                    if task["kind"] == "generate" and task["status"] == "failed"),
                          "ideas": len(control), "mechanisms": len(first_batch), "curve": curve},
              "queued_for_grouping": sum(len(entry["idea_ids"]) for entry in state["queue"]),
              "failures": state["failures"]}
    if "merge" in state:
        result["skill"] = {**state["merge"]["source_summary"], "merge_finished": state["merge"]["done"]}
        result["same_grouper"] = {**sides, "skill_only_by_first_level": skill_only_by_level,
                                  "skill_ideas_that_landed_in_a_control_mechanism_by_level": landed_in_control}
    return result


def cmd_compare(args):
    emit(comparison(bs.load_state(args.dir)))


def cmd_check(args):
    state = bs.load_state(args.dir)
    problems = []
    if not state["complete"]:
        problems.append("the run is not finished")
    dropped = [row for row in state["failures"] if row["reason"] in ("missing_output", "invalid_output")]
    if len(dropped) > args.max_dropped:
        problems.append(f"{len(dropped)} idea agents were dropped (allowed: {args.max_dropped})")
    ungrouped = sum(1 for row in state["mechanisms"] if row.get("ungrouped"))
    if ungrouped:
        problems.append(f"{ungrouped} ideas were left ungrouped")
    levels = [row for row in state["summary"] if "ideas" in row]
    expected = ["baseline"] + bs.PROFILES[state["profile"]]
    if [row["level"] for row in levels] != expected and state["pressure"] != "stop":
        problems.append(f"levels run: {[row['level'] for row in levels]}, expected {expected}")
    for row in levels:
        if row["level"] != "baseline" and row["new_mechanisms_beyond_baseline"] < args.min_new:
            problems.append(f"{row['level']} added {row['new_mechanisms_beyond_baseline']} mechanisms beyond the "
                            f"baseline (floor: {args.min_new})")
    emit({"ok": not problems, "problems": problems, "levels": levels, "ideas": len(state["ideas"]),
          "mechanisms": len(state["mechanisms"])})
    return 0 if not problems else 1


SAME_RULE = ("Two mechanisms are the same when the thing that makes them work is the same: who acts, what they do, "
             "and why that produces the result. A different channel, audience, wording, scale or packaging alone "
             "is still the same mechanism. If you cannot decide, answer that they are different.")
SIDES = ("skill_only", "control_only", "both")


def mechanism_side(state, mechanism):
    imported = [state["ideas"][key]["level"].startswith(IMPORT_PREFIX) for key in mechanism["idea_ids"]]
    return "skill_only" if all(imported) else "both" if any(imported) else "control_only"


def audit_paths(run_dir):
    folder = Path(run_dir).resolve() / "audit"
    return folder, folder / "prompt.md", folder / "key.json", folder / "answer.json"


def cmd_audit_init(args):
    state = bs.load_state(args.dir)
    if not state.get("merge", {}).get("done"):
        raise bs.RunError("merge a skill run and finish its grouping first")
    rng = random.Random(args.seed)
    sides = {row["mechanism_id"]: mechanism_side(state, row) for row in state["mechanisms"]}
    known = [row for row in state["mechanisms"] if sides[row["mechanism_id"]] != "skill_only"]
    alone = [row for row in state["mechanisms"] if sides[row["mechanism_id"]] == "skill_only"]
    sample = rng.sample(alone, min(args.sample, len(alone)))
    mixed = rng.sample(state["mechanisms"], len(state["mechanisms"]))
    folder, prompt_path, key_path, answer_path = audit_paths(args.dir)

    def lines(prefix, rows):
        out = []
        for number, row in enumerate(rows, 1):
            idea = state["ideas"][row["representative"]]
            out.extend([f"{prefix}{number}. {row['label']}", f"   Example: {idea['title']} - {idea['text']}", ""])
        return out

    constraints = [f"{number}. {text}" for number, text in enumerate(state["constraints"], 1)]
    parts = ["# Audit", "", "Read only this file. Do not search or read anything else.",
             f"Write one JSON object to {answer_path} and change no other file.", "",
             "Question:", state["question"], "", "Hard constraints:", *(constraints or ["(none)"]), "",
             "# Part 1: repeats", "", SAME_RULE, "",
             "For each B item, name the one A item it is the same as, or null.", "",
             f"## A: {len(known)} known mechanisms", "", *lines("A", known),
             f"## B: {len(sample)} mechanisms to check", "", *lines("B", sample),
             "# Part 2: hard constraints", "",
             "For each C item, list the numbers of the hard constraints that its example breaks. "
             "Use an empty list when it breaks none or when you cannot decide.", "",
             f"## C: {len(mixed)} mechanisms", "", *lines("C", mixed),
             "# Output", "",
             '{"repeats": [{"b": "B1", "same_as": "A3" or null, "note": "one sentence"}, ...one per B item], '
             '"constraints": [{"c": "C1", "violated": [], "note": "one sentence when not empty"}, ...one per C item]}']
    key = {"rule": SAME_RULE, "skill_only": len(alone),
           "A": {f"A{n}": row["mechanism_id"] for n, row in enumerate(known, 1)},
           "B": {f"B{n}": row["mechanism_id"] for n, row in enumerate(sample, 1)},
           "C": {f"C{n}": {"mechanism_id": row["mechanism_id"], "side": sides[row["mechanism_id"]]}
                 for n, row in enumerate(mixed, 1)}}
    folder.mkdir(exist_ok=True)
    prompt_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    key_path.write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    emit({"prompt": str(prompt_path), "answer": str(answer_path), "sampled": len(sample), "skill_only": len(alone),
          "known": len(known), "next": "give the prompt file to one judge agent of a different model, then run audit-read"})


def cmd_audit_read(args):
    state = bs.load_state(args.dir)
    _, _, key_path, answer_path = audit_paths(args.dir)
    if not key_path.exists() or not answer_path.exists():
        raise bs.RunError("run audit-init and have the judge write its answer first")
    key = json.loads(key_path.read_text(encoding="utf-8"))
    answer = json.loads(answer_path.read_text(encoding="utf-8"))
    problems = []
    repeats = {row.get("b"): row.get("same_as") for row in answer.get("repeats", []) if isinstance(row, dict)}
    for label in key["B"]:
        if label not in repeats:
            problems.append(f"no answer for {label}")
        elif repeats[label] is not None and repeats[label] not in key["A"]:
            problems.append(f"{label} names an unknown item: {repeats[label]}")
    judged = {row.get("c"): row.get("violated") for row in answer.get("constraints", []) if isinstance(row, dict)}
    allowed = set(range(1, len(state["constraints"]) + 1))
    violations = dict.fromkeys(SIDES, 0)
    for label, row in key["C"].items():
        value = judged.get(label)
        if label not in judged:
            problems.append(f"no answer for {label}")
        elif not isinstance(value, list) or not set(value) <= allowed:
            problems.append(f"{label} lists something that is not a constraint number: {value}")
        elif value:
            violations[row["side"]] += 1
    same = sum(1 for label in key["B"] if repeats.get(label) in key["A"])
    sampled = len(key["B"])
    emit({"ok": not problems, "problems": problems, "rule": key["rule"],
          "repeats": {"sampled": sampled, "judged_same": same, "skill_only_by_grouper": key["skill_only"],
                      "estimated_new": round(key["skill_only"] * (sampled - same) / sampled) if sampled else 0},
          "constraints": {"checked": len(key["C"]), "violations": violations}})
    return 0 if not problems else 1


def cmd_repeat(args):
    """Compare separate groupings of the same merged ideas: how far the counts move with the grouping alone."""
    if len(args.dir) < 2:
        raise bs.RunError("give --dir at least twice: one control run per grouping of the same skill run")
    runs = []
    for run_dir in args.dir:
        state = bs.load_state(run_dir)
        if not state.get("merge", {}).get("done"):
            raise bs.RunError(f"{run_dir}: merge a skill run and finish its grouping first")
        sides = [mechanism_side(state, row) for row in state["mechanisms"]]
        new = {key: side == "skill_only" for row, side in zip(state["mechanisms"], sides)
               for key in row["idea_ids"] if state["ideas"][key]["level"].startswith(IMPORT_PREFIX)}
        runs.append({"dir": str(run_dir), "question": state["question"], "new": new,
                     "counts": {side: sides.count(side) for side in SIDES}})
    first = runs[0]
    for run in runs[1:]:
        if run["question"] != first["question"] or set(run["new"]) != set(first["new"]):
            raise bs.RunError(f"{run['dir']} does not hold the same merged ideas as {first['dir']}")
    keys = sorted(first["new"])
    pairs = []
    for a, b in itertools.combinations(runs, 2):
        same = sum(a["new"][key] == b["new"][key] for key in keys)
        pairs.append({"first": a["dir"], "second": b["dir"], "ideas": len(keys), "same_verdict": same,
                      "rate": round(same / len(keys), 3),
                      "new_in_first_only": sum(a["new"][key] and not b["new"][key] for key in keys),
                      "new_in_second_only": sum(b["new"][key] and not a["new"][key] for key in keys)})
    votes = [sum(run["new"][key] for run in runs) for key in keys]
    counts = [run["counts"]["skill_only"] for run in runs]
    emit({"runs": [{"dir": run["dir"], **run["counts"], "ideas_outside_control": sum(run["new"].values())}
                   for run in runs],
          "skill_only_range": [min(counts), max(counts)], "pairs": pairs,
          "ideas": {"total": len(keys), "new_in_every_grouping": votes.count(len(runs)),
                    "new_in_none": votes.count(0), "split": sum(0 < vote < len(runs) for vote in votes)}})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("direct-init")
    init.add_argument("--dir", required=True)
    init.add_argument("--question-file", required=True)
    init.add_argument("--constraint", action="append")
    init.add_argument("--batches", type=int, required=True, help="Batches of 3 agents; 12 batches = 36 idea calls")
    init.add_argument("--ideas-per-agent", type=int, default=bs.DEFAULT_IDEAS_PER_AGENT)
    init.add_argument("--seed", type=int)
    init.set_defaults(func=cmd_direct_init)
    step = commands.add_parser("next")
    step.add_argument("--dir", required=True)
    step.add_argument("--give-up", action="store_true")
    step.set_defaults(func=cmd_next)
    merge = commands.add_parser("merge")
    merge.add_argument("--dir", required=True)
    merge.add_argument("--from", dest="source", required=True, help="Run directory of a finished skill run")
    merge.set_defaults(func=cmd_merge)
    compare = commands.add_parser("compare")
    compare.add_argument("--dir", required=True)
    compare.set_defaults(func=cmd_compare)
    check = commands.add_parser("check")
    check.add_argument("--dir", required=True)
    check.add_argument("--max-dropped", type=int, default=0)
    check.add_argument("--min-new", type=int, default=10,
                       help="Floor for new mechanisms beyond the baseline at each level (observed so far: 13 to 28)")
    check.set_defaults(func=cmd_check)
    audit = commands.add_parser("audit-init", help="Write a blind prompt for one judge agent of a different model")
    audit.add_argument("--dir", required=True, help="Control run with a merged skill run")
    audit.add_argument("--sample", type=int, default=25, help="Skill-only mechanisms to check for repeats")
    audit.add_argument("--seed", type=int, default=0)
    audit.set_defaults(func=cmd_audit_init)
    read = commands.add_parser("audit-read")
    read.add_argument("--dir", required=True)
    read.set_defaults(func=cmd_audit_read)
    repeat = commands.add_parser("repeat", help="Compare two or more groupings of the same merged skill run")
    repeat.add_argument("--dir", action="append", required=True, help="Control run with a finished merge; repeat")
    repeat.set_defaults(func=cmd_repeat)
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except (bs.RunError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
