"""Recoverable generation scheduling over the recording and mechanism authorities."""

from copy import deepcopy
import fcntl
from pathlib import Path
import random
import sys

from . import dedupe, record, replacements
from .allocate import allocate
from .backends import NotStarted
from .budget import BudgetExceeded, BudgetLedger, atomic_write, cli_boundary, default_ledger_path, dumps
from .config import LEVELS, load_config
from .exam import load_exams


def save_state(path, state):
    """Commit scheduling first, then its recoverable public batch projection."""
    # ceiling: whole-unit snapshots suit small evaluations; index when IO dominates calls.
    rows = [{key: value for key, value in batch.items() if key in BATCH_FIELDS}
            for batch in state["batches"]]
    updated = {**state, "previous_projection": state.get("projection", []), "projection": rows}
    atomic_write(path / "loop_state.json", dumps({"digest": record.digest(updated), "state": updated}) + "\n")
    atomic_write(path / "loop_batches.jsonl", "".join(dumps(row) + "\n" for row in rows))
    # Retain the durable predecessor even if projection writes fail more than once.
    state.update(previous_projection=updated["previous_projection"], projection=rows)


BATCH_FIELDS = {"exam_id", "config", "rep", "batch", "level", "kind", "status", "idea_ids",
                "returned_idea_ids", "new_mechanism_ids", "new_mechanisms", "authority_new_mechanism_ids",
                "fallback_ids", "stop_reason", "fuel"}


def load_state(path, identity):
    file = path / "loop_state.json"
    if not file.exists():
        if any((path / name).exists() for name in ("record_state.json", "ideas.jsonl", "calls.jsonl",
                                                  "loop_batches.jsonl", "run_summary.json", "failures.jsonl")):
            raise ValueError("Missing loop state with surviving recording outputs; use a new loop unit")
        return {"identity": identity, "batches": [], "next_batch": 1, "cursor": 0,
                "complete": False, "stop_reason": None, "projection": []}
    envelope = record.read_file(file)
    if not isinstance(envelope, dict) or "state" not in envelope or envelope.get("digest") != record.digest(envelope["state"]):
        raise ValueError("Invalid loop state digest")
    state = envelope["state"]
    if state["identity"] != identity:
        raise ValueError("Loop identity conflict: policy, exam, config, seed, ledger or classifier changed; preserve old outputs and use a new unit")
    if (path / "loop_batches.jsonl").exists():
        rows = record.read_rows(path / "loop_batches.jsonl")
        if rows not in (state["projection"], state.get("previous_projection", [])):
            raise ValueError("Conflicting loop batch projection")
        state["projection"] = rows
    return state


def classifier_state(run_dir, exam, ledger, backend, config):
    path = dedupe.stage_path(run_dir, exam["id"], None)
    with record.recording_lock(path / "dedupe"):
        return dedupe.load_state(path, exam["id"], ledger,
                                 {"backend": backend.name, "model": config["default_model"]})


def batch_row(exam, config, rep, number, level):
    return {"exam_id": exam["id"], "config": config["name"], "rep": rep, "batch": number,
            "level": level, "kind": "baseline" if level == "baseline" else "generation",
            "status": "not_evaluated", "idea_ids": [], "returned_idea_ids": [],
            "new_mechanism_ids": [], "new_mechanisms": None, "authority_new_mechanism_ids": [],
            "fallback_ids": [], "stop_reason": None, "stage": "generate", "rewrites": []}


def unit_view(authority, config, rep):
    """Project shared membership onto this unit without changing authoritative IDs."""
    selected = [row for row in authority["ideas"].values() if row["config"] == config["name"] and row["rep"] == rep]
    selected.sort(key=lambda row: (row["batch"], row["agent"], row["idea_id"]))
    ideas = {row["idea_id"]: row for row in selected}
    order = {key: index for index, key in enumerate(ideas)}
    table = []
    for row in authority["table"]["mechanisms"]:
        members = sorted(set(row["idea_ids"]).intersection(ideas), key=order.get)
        if members:
            table.append({**row, "idea_ids": members, "representative": members[0]})
    table.sort(key=lambda row: order[row["representative"]])
    return {"ideas": ideas, "table": {"exam_id": authority["table"]["exam_id"], "mechanisms": table}}


def generation_context(authority, level, config, *, returned=False):
    visible = level not in ("baseline", "round1")
    ideas = authority["ideas"]
    table = authority["table"]["mechanisms"]
    decompositions = [{"idea_id": key, "decomposition": row["decomposition"]}
                      for key, row in ideas.items() if row["level"] != "baseline" and row["decomposition"]]
    singletons = [ideas[row["idea_ids"][0]] for row in table if len(row["idea_ids"]) == 1
                  and ideas[row["idea_ids"][0]]["level"] != "baseline"]
    # Global numbers and labels depend on other units' arrival order; expose only local descriptions.
    context = {"forbidden": [{key: ideas[row["representative"]][key] for key in ("title", "text")} for row in table]
                         if visible or returned or (level == "round1" and config["before_generation"]) else None,
            "decompositions": decompositions if visible else None,
            "previous": [{key: row[key] for key in ("title", "text")} for row in singletons] if visible else None}
    if level == "pressure3":
        dimensions = {}
        for key, row in ideas.items():
            if row["level"] != "baseline":
                for dimension in set(row["decomposition"] or []):
                    dimensions.setdefault(dimension, []).append(key)
        context["premises"] = {"status": "observed" if dimensions else "missing_data", "dimensions": [
            {"dimension": name, "count": len(ids), "idea_ids": sorted(ids)}
            for name, ids in sorted(dimensions.items(), key=lambda pair: (-len(pair[1]), pair[0]))]}
    return context


def pressure_assignments(exam, config, path, seed, rep, batch, level):
    allocation_seed = int(record.digest([seed, rep, batch, level]), 16)
    assignments = allocate(exam, config, allocation_seed)
    if level == "baseline":
        for row in assignments:
            row["method"] = None
    if level not in ("pressure2", "pressure4"):
        return assignments, [], None
    recorded = record.load_state(path, None)
    sent = {row["call_key"] for row in recorded["calls"]}
    used_cards, used_methods = set(), set()
    for key, request in recorded["requests"].items():
        if key not in sent:
            continue
        saved = recorded["batches"][f"{request['batch']}:{request['level']}"]["assignments"]
        assignment = next(row for row in saved if row["agent"] == request["agent"])
        used_cards.update(assignment["cards"])
        if assignment["method"] is not None:
            used_methods.add(assignment["method"])
    rng = random.Random(allocation_seed)
    fuel = []
    if level == "pressure2":
        cards = sorted(card["id"] for card in exam["card_data"])
        new, old = [key for key in cards if key not in used_cards], [key for key in cards if key in used_cards]
        rng.shuffle(new)
        rng.shuffle(old)
        count = len(assignments[0]["cards"])
        # All agents get only fresh fuel when enough exists; scarce fresh cards are covered first.
        for index, row in enumerate(assignments):
            fresh = ([new[(index * count + n) % len(new)] for n in range(min(count, len(new)))] if new else [])
            selected = fresh + old[:count - len(fresh)]
            row["cards"] = sorted(selected)
            fuel.append({"agent": row["agent"], "new": sorted(fresh), "reused": sorted(set(selected) - set(fresh))})
    if level == "pressure4":
        if config["method_mode"] == "none":
            return [], fuel, "methods_disabled"
        methods = sorted(set(config["methods"]) - used_methods)
        if not methods:
            return [], fuel, "no_unused_methods"
        rng.shuffle(methods)
        for index, row in enumerate(assignments):
            row["method"] = methods[0 if config["method_mode"] == "same" else index % len(methods)]
    return assignments, fuel, None


def record_step(step, exam, config, run_dir, path, seed, backend, ledger, rep, level):
    record.record_batch(exam, config, run_dir, seed=seed, backend=backend, ledger=ledger,
                        rep=rep, batch=step["batch"], level=level, **step["inputs"])
    recorded = record.load_state(path, None)
    ids = [row["idea_id"] for row in recorded["ideas"] if row["batch"] == step["batch"]]
    requests = [r for r in recorded["requests"].values() if r.get("batch") == step["batch"]]
    latest = {}
    for request in requests:
        agent = request["agent"]
        if agent not in latest or request["attempt"] > latest[agent]["attempt"]:
            latest[agent] = request
    if not latest or any(request["status"] != "ok" for request in latest.values()):
        raise ValueError("Generation failed; not an observed exhaustion")
    return ids


def group_ids(ids, exam, run_dir, path, seed, backend, ledger, config):
    recorded = record.load_state(path, None)
    wanted = set(ids)
    return dedupe.deduplicate([row for row in recorded["ideas"] if row["idea_id"] in wanted], run_dir,
        exam_id=exam["id"], seed=seed, backend=backend, ledger=ledger,
        model=config["default_model"], max_output_tokens=config["max_output_tokens"])


def advance(state, batch, levels, config):
    if batch["level"] == "baseline":
        if len(state["batches"]) == 3 and not levels:
            state.update(complete=True, stop_reason="baseline_only")
            batch["stop_reason"] = "baseline_only"
        return
    growth = batch["new_mechanisms"]
    count = sum(row["level"] == batch["level"] for row in state["batches"])
    if growth == 0 and config["stop_on_exhaustion"]:
        state.update(complete=True, stop_reason="stop_on_exhaustion")
    elif growth == 0 or count >= config["max_batches"]:
        state["cursor"] += 1
        if state["cursor"] == len(levels):
            state.update(complete=True, stop_reason="exhausted" if growth == 0 else "max_batches")
    batch["stop_reason"] = state["stop_reason"]


def loop_identity(exam, config, seed, rep, ledger, backend, dedupe_config, dedupe_backend):
    effective = lambda value: {k: v for k, v in value.items() if k != "budget_usd"}
    return {"loop_policy": "unit-local-baseline-v1", "exam": record.digest(exam),
            "config": record.digest(effective(config)), "seed": seed, "rep": rep,
            "ledger": str(ledger.path), "backend": backend.name,
            "dedupe_config": record.digest(effective(dedupe_config)), "dedupe_backend": dedupe_backend.name}


def run_loop(exam, config, run_dir, *, seed, backend, ledger, dedupe_config, dedupe_backend, rep=1):
    """Run one exam/configuration/repetition using one fixed authoritative classifier."""
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if (backend.name == "fake") != (dedupe_backend.name == "fake"):
        raise ValueError("Generation and dedupe cannot mix fake and real billing")
    if dedupe_config["default_model"] not in dedupe_config["models"]:
        raise ValueError("Classifier model must be allowed by dedupe config")
    identity = loop_identity(exam, config, seed, rep, ledger, backend, dedupe_config, dedupe_backend)
    path = record.output_dir(run_dir, exam["id"], config["name"], rep)
    replacements.refuse_if_retired(run_dir, exam["id"], config["name"], rep)
    path.mkdir(parents=True, exist_ok=True)
    # ceiling: serialize one unit across model calls; split reservations only if unit parallelism is needed.
    with (path / "loop.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(path, identity)
        classifier_state(run_dir, exam, ledger, dedupe_backend, dedupe_config)
        record.load_state(path, {key: identity[key] for key in ("exam", "config", "seed", "ledger", "backend")})
        levels = [level for level in LEVELS if level != "baseline" and level in config["levels"]]
        save_state(path, state)
        while not state["complete"]:
            if not state["batches"] or state["batches"][-1]["stage"] == "done":
                level = "baseline" if len(state["batches"]) < 3 else levels[state["cursor"]]
                authority = classifier_state(run_dir, exam, ledger, dedupe_backend, dedupe_config)
                table = authority["table"]["mechanisms"]
                local = unit_view(authority, config, rep)
                batch = batch_row(exam, config, rep, state["next_batch"], level)
                state["next_batch"] += 1
                batch["authority_before"] = [row["mechanism_id"] for row in table]
                batch["unit_before"] = [row["mechanism_id"] for row in local["table"]["mechanisms"]]
                batch["reference_ids"] = [key for key, row in local["ideas"].items() if row["level"] == "baseline"]
                batch["inputs"] = generation_context(local, level, config)
                assignments, fuel, unavailable = pressure_assignments(exam, config, path, seed, rep, batch["batch"], level)
                batch["inputs"]["assignments"] = assignments
                batch["fuel"] = fuel
                if unavailable:
                    batch.update(stage="done", stop_reason=unavailable)
                    state.update(complete=True, stop_reason=unavailable)
                state["batches"].append(batch)
                save_state(path, state)
                if unavailable:
                    print(f"Loop {exam['id']}/{config['name']} rep={rep} level={level} batch={batch['batch']} "
                          f"stage=not_run stop_reason={unavailable}", file=sys.stderr)
                    break
            batch = state["batches"][-1]
            if batch["stop_reason"] is not None:
                batch["stop_reason"] = None
                save_state(path, state)
            print(f"Loop {exam['id']}/{config['name']} rep={rep} level={batch['level']} "
                  f"batch={batch['batch']} stage={batch['stage']}", file=sys.stderr)
            try:
                if batch["stage"] == "generate":
                    batch["idea_ids"] = record_step(batch, exam, config, run_dir, path, seed, backend, ledger, rep, batch["level"])
                    batch["stage"] = "dedupe"
                    save_state(path, state)
                if batch["stage"] == "dedupe":
                    result = group_ids(batch["idea_ids"], exam, run_dir, path, seed, dedupe_backend, ledger, dedupe_config)
                    batch["fallback_ids"] = sorted(set(batch["idea_ids"]).intersection(result["fallback_ids"]))
                    if batch["fallback_ids"]:
                        raise ValueError("Incomplete mechanism grouping; growth not evaluated")
                    if batch["level"] != "baseline" and config["return_collisions"]:
                        collided = dedupe.collisions(result["table"], batch["reference_ids"], batch["idea_ids"])
                        recorded = {row["idea_id"]: row for row in record.load_state(path, None)["ideas"]}
                        local = unit_view(classifier_state(run_dir, exam, ledger, dedupe_backend, dedupe_config), config, rep)
                        context = generation_context(local, batch["level"], config, returned=True)
                        for key in collided:
                            original = recorded[key]
                            batch["rewrites"].append({"batch": state["next_batch"], "stage": "generate", "idea_ids": [],
                                "inputs": {**deepcopy(context), "returned_from": key, "returned_idea": original,
                                           "assignments": [{k: original[k] for k in ("agent", "model", "method", "cards")}],
                                           "purpose": "rewrite"}})
                            state["next_batch"] += 1
                    batch["stage"] = "rewrite"
                    save_state(path, state)
                if batch["stage"] == "rewrite":
                    for step in batch["rewrites"]:
                        if step["stage"] == "generate":
                            step["idea_ids"] = record_step(step, exam, config, run_dir, path, seed, backend, ledger, rep, batch["level"])
                            step["stage"] = "dedupe"
                            batch["returned_idea_ids"] = [key for row in batch["rewrites"] for key in row["idea_ids"]]
                            save_state(path, state)
                        if step["stage"] == "dedupe":
                            result = group_ids(step["idea_ids"], exam, run_dir, path, seed, dedupe_backend, ledger, dedupe_config)
                            bad = set(step["idea_ids"]).intersection(result["fallback_ids"])
                            batch["fallback_ids"] = sorted(set(batch["fallback_ids"]) | bad)
                            if bad:
                                raise ValueError("Incomplete rewrite grouping; growth not evaluated")
                            step["stage"] = "done"
                            save_state(path, state)
                    batch["stage"] = "finalize"
                    save_state(path, state)
                if batch["stage"] == "finalize":
                    authority = classifier_state(run_dir, exam, ledger, dedupe_backend, dedupe_config)
                    table = authority["table"]["mechanisms"]
                    ids = set(batch["idea_ids"] + batch["returned_idea_ids"])
                    members = [row for row in table if ids.intersection(row["idea_ids"])]
                    batch["new_mechanism_ids"] = [row["mechanism_id"] for row in members
                        if batch["level"] != "baseline" and row["mechanism_id"] not in batch["unit_before"]]
                    # Membership is append-only; only the opening batch owns the global addition.
                    batch["authority_new_mechanism_ids"] = [row["mechanism_id"] for row in members
                        if row["mechanism_id"] not in batch["authority_before"] and row["idea_ids"][0] in ids]
                    batch.update(status="ok", new_mechanisms=len(batch["new_mechanism_ids"]), stage="done", stop_reason=None)
                    advance(state, batch, levels, config)
                    save_state(path, state)
                    print(f"Loop {exam['id']}/{config['name']} rep={rep} level={batch['level']} "
                          f"batch={batch['batch']} stage=done new_mechanisms={batch['new_mechanisms']} "
                          f"stop_reason={state['stop_reason']}", file=sys.stderr)
            except Exception as exc:
                if batch["stage"] != "done":
                    batch["stop_reason"] = type(exc).__name__
                if batch["stage"] in ("generate", "rewrite") and (path / "record_state.json").exists():
                    recorded = record.load_state(path, None)
                    batch["idea_ids"] = [row["idea_id"] for row in recorded["ideas"] if row["batch"] == batch["batch"]]
                    rewrite_batches = {step["batch"] for step in batch["rewrites"]}
                    batch["returned_idea_ids"] = [row["idea_id"] for row in recorded["ideas"] if row["batch"] in rewrite_batches]
                save_state(path, state)
                print(f"Loop stopped exam={exam['id']} config={config['name']} rep={rep} level={batch['level']} "
                      f"batch={batch['batch']} stage={batch['stage']} reason={type(exc).__name__}", file=sys.stderr)
                raise
        return {"exam_id": exam["id"], "config": config["name"], "rep": rep,
                "complete": state["complete"], "stop_reason": state["stop_reason"],
                "batches_path": str(path / "loop_batches.jsonl")}


def run_cli(args):
    config, classifier = load_config(args.config), load_config(args.dedupe_config)
    run_dir = args.run_dir or Path(__file__).resolve().parent / "runs" / config["name"]
    if Path(run_dir).resolve().is_relative_to(args.exam_dir.resolve()):
        raise ValueError("run-dir must not be inside the read-only exam-dir")
    backend, grouping = record.make_backend(config), record.make_backend(classifier)
    if classifier["backend"]["kind"] == "fake" and "responses" not in classifier["backend"]:
        grouping.responses = dedupe.fake_partition
    ledger = BudgetLedger(args.ledger or default_ledger_path(backend.name, run_dir), args.limit or config["budget_usd"])
    exams = [exam for exam in load_exams(args.exam_dir) if args.exam_id is None or exam["id"] == args.exam_id]
    if not exams:
        raise ValueError("exam-id was not found")
    for exam in exams:
        try:
            result = run_loop(exam, config, run_dir, seed=args.seed, backend=backend, ledger=ledger,
                              dedupe_config=classifier, dedupe_backend=grouping, rep=args.rep)
        except BudgetExceeded:
            print("For a reservation limit, rerun the same arguments with a higher --limit and the same ledger. "
                  "For an actual over-reserve halt, raising --limit cannot clear it; reconcile billing first. "
                  "Preserve all recording, loop and ledger files.", file=sys.stderr)
            raise
        except NotStarted as exc:
            raise ValueError(f"Loop stopped before provider startup: {exc}") from exc
        print(dumps(result))


def register(subparsers):
    parser = subparsers.add_parser("loop", help="Record baseline, rounds, collision returns and pressure levels")
    parser.add_argument("--exam-dir", type=Path, required=True)
    parser.add_argument("--exam-id")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dedupe-config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--limit")
    parser.set_defaults(func=lambda args: cli_boundary(run_cli, args))
