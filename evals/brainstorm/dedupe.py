"""Incremental mechanism partitions, isolated replay, and human calibration."""

from collections import Counter
from copy import deepcopy
from itertools import combinations
import json
from pathlib import Path
import random
import re
import sys

from .ami import adjusted_mutual_info, partition_labels
from .backends import NotStarted
from .budget import (BudgetLedger, LedgerError, PaidCallError, atomic_write, cli_boundary,
                     default_ledger_path, dumps, guarded_call)
from .config import load_config, positive_int
from .exam import safe_segment
from .prompts import wrap_data
from .record import (MeasuredBackend, digest, make_backend, read_file, read_rows,
                     recording_lock, sync_call, validate_idea)


MAX_PAID_ATTEMPTS = 2


def checked_ideas(ideas, exam_id):
    result = {}
    for row in ideas:
        validate_idea(row)
        key = row["idea_id"]
        if row["exam_id"] != exam_id or key in result:
            raise ValueError("Dedupe requires unique ideas from exactly one exam")
        result[key] = deepcopy(row)
    return result


def validate_table(table, ideas, exam_id):
    if not isinstance(table, dict) or set(table) != {"exam_id", "mechanisms"} or table["exam_id"] != exam_id:
        raise ValueError("Invalid mechanism table exam or fields")
    if not isinstance(table["mechanisms"], list):
        raise ValueError("Mechanisms must be an array")
    mechanisms, members = set(), []
    for row in table["mechanisms"]:
        if (not isinstance(row, dict) or set(row) != {"mechanism_id", "label", "idea_ids", "in_baseline", "representative"}
                or not isinstance(row["mechanism_id"], str) or not re.fullmatch(r"m-[0-9]{3,}", row["mechanism_id"])
                or row["mechanism_id"] in mechanisms or int(row["mechanism_id"][2:]) < 1
                or not isinstance(row["label"], str) or not row["label"].strip()
                or not isinstance(row["idea_ids"], list) or not row["idea_ids"]
                or any(not isinstance(key, str) or key not in ideas for key in row["idea_ids"])
                or row["representative"] not in row["idea_ids"] or type(row["in_baseline"]) is not bool):
            raise ValueError("Invalid mechanism identity, representative, or members")
        if row["in_baseline"] != any(ideas[key]["level"] == "baseline" for key in row["idea_ids"]):
            raise ValueError("Mechanism baseline marker conflicts with members")
        mechanisms.add(row["mechanism_id"])
        members.extend(row["idea_ids"])
    if Counter(members) != Counter(ideas.keys()):
        raise ValueError("Mechanisms must partition every idea exactly once")


def stage_path(run_dir, exam_id, replay):
    root = Path(run_dir).resolve()
    safe_segment(exam_id)
    path = root / exam_id if replay is None else root / "replay" / safe_segment(replay) / exam_id
    # Reject aliases even within replay: a second name must not reopen another partition.
    resolved = path.resolve()
    if resolved != path or not resolved.is_relative_to(root):
        raise ValueError("Dedupe output path contains an alias or escapes run-dir")
    for child in (path / "dedupe", path / "mechanisms.json", path / "dedupe/state.json",
                  path / "dedupe/calls.jsonl", path / "dedupe/failures.jsonl", path / "dedupe/record.lock"):
        if child.resolve() != child:
            raise ValueError("Dedupe output files cannot be symlink aliases")
    return path


def save_state(path, state):
    atomic_write(path / "dedupe/state.json", dumps({"digest": digest(state), "state": state}) + "\n")


def load_state(path, exam_id, ledger, classifier):
    state_path = path / "dedupe/state.json"
    if not state_path.exists():
        if any((path / filename).exists() for filename in
               ("mechanisms.json", "dedupe/calls.jsonl", "dedupe/failures.jsonl")):
            raise ValueError("Missing dedupe state with surviving outputs")
        return {"exam_id": exam_id, "ledger": str(ledger.path), "classifier": classifier, "ideas": {},
                "table": {"exam_id": exam_id, "mechanisms": []}, "requests": {},
                "calls": [], "failures": [], "fallback_ids": [], "pending": None}
    envelope = read_file(state_path)
    if not isinstance(envelope, dict) or "state" not in envelope or envelope.get("digest") != digest(envelope["state"]):
        raise ValueError("Invalid dedupe state digest")
    state = envelope["state"]
    if state["exam_id"] != exam_id or state["ledger"] != str(ledger.path):
        raise ValueError("Dedupe exam or ledger identity conflict")
    if state.get("classifier") != classifier:
        raise ValueError("Dedupe classifier identity conflict; use an independent replay to change models")
    checked = checked_ideas(list(state["ideas"].values()), exam_id)
    if checked != state["ideas"]:
        raise ValueError("Invalid dedupe idea identity map")
    validate_table(state["table"], checked, exam_id)
    if (path / "mechanisms.json").exists():
        table = read_file(path / "mechanisms.json")
        if table != state["table"] and table != state.get("previous_table"):
            raise ValueError("Corrupt or conflicting persisted mechanisms")
    elif state["ideas"] and "previous_table" not in state:
        raise ValueError("Missing committed mechanism table")
    return state


def export_outputs(path, state):
    # ceiling: whole-exam projections suit small evaluations; index when rewrite IO dominates.
    atomic_write(path / "mechanisms.json", dumps(state["table"]) + "\n")
    for kind in ("calls", "failures"):
        rows = state[kind]
        if kind == "failures":
            rows = [row for row in rows if row["reason"] == "invalid_partition"
                    or row["call_key"] == state["pending"] or row["call_key"] in state.get("abandoned", [])]
        atomic_write(path / f"dedupe/{kind}.jsonl", "".join(dumps(row) + "\n" for row in rows))
    state.pop("previous_table", None)
    save_state(path, state)


def mechanism_prompt(batch, table, ideas):
    visible = [{"mechanism_id": row["mechanism_id"], "label": row["label"],
                "variants": [{key: ideas[item][key] for key in ("title", "text")}
                             for item in row["idea_ids"]]} for row in table["mechanisms"]]
    return "\n".join([
        "Group ideas by mechanism: the actual way an idea works, not its wording. Only merge equivalent mechanisms.",
        'For example, "周六下午专场" and "周末青少年专属时段" describe the same mechanism.',
        "Assign every supplied new idea exactly once to an existing mechanism or a new group. Keep existing groups immutable.",
        "Write each new label in the same language as the ideas.",
        'Return only JSON: {"groups":[{"target":"m-001 or new:unique_tag","label":"mechanism description","idea_ids":["i-..."]}]}.',
        "Existing mechanisms:", wrap_data(visible), "New ideas:",
        wrap_data([{key: row[key] for key in ("idea_id", "title", "text")} for row in batch])])


def parse_partition(text, batch, table):
    """Preserve unambiguous assignments; every affected idea gets its own bucket."""
    ids = {row["idea_id"] for row in batch}
    existing = {row["mechanism_id"] for row in table["mechanisms"]}
    assignments = {key: [] for key in ids}
    errors = []

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    try:
        payload = json.loads(text, object_pairs_hook=unique_object)
        if not isinstance(payload, dict) or set(payload) != {"groups"} or not isinstance(payload["groups"], list):
            raise ValueError("Expected a groups array")
        for row in payload["groups"]:
            if not isinstance(row, dict) or not isinstance(row.get("idea_ids"), list):
                errors.append("Invalid group shape")
                continue
            target, label = row.get("target"), row.get("label")
            valid = (set(row) == {"target", "label", "idea_ids"} and isinstance(target, str)
                     and (target in existing or target.startswith("new:") and len(target) > 4)
                     and isinstance(label, str) and bool(label.strip()))
            for key in row["idea_ids"]:
                if not isinstance(key, str) or key not in ids:
                    errors.append(f"Unknown idea ID: {key!r}")
                else:
                    assignments[key].append((target, label) if valid else None)
    except (ValueError, RecursionError) as exc:
        errors.append(f"Invalid grouping JSON: {exc}")
        assignments = {key: [] for key in ids}
    labels = {}
    for entries in assignments.values():
        for entry in entries:
            if entry is not None:
                labels.setdefault(entry[0], set()).add(entry[1])
    buckets = {}
    fallback = []
    for row in batch:
        key = row["idea_id"]
        entries = assignments[key]
        if len(entries) != 1 or entries[0] is None or (entries[0][0].startswith("new:") and len(labels[entries[0][0]]) != 1):
            target, label = "fallback:" + key, row["title"]
            fallback.append(key)
            errors.append(f"Missing, duplicate, or invalid assignment for {key}")
        else:
            target, label = entries[0]
        bucket = buckets.setdefault(target, {"target": target, "label": label, "idea_ids": []})
        bucket["idea_ids"].append(key)
    return list(buckets.values()), fallback, errors


def deduplicate(ideas, run_dir, *, exam_id, seed, backend, ledger, model,
                batch_size=40, max_output_tokens=2048, replay=None, retry_paid_error=False):
    """Return the table, new mechanism IDs, fallback IDs, and owned output paths."""
    positive_int(batch_size, "batch_size")
    positive_int(max_output_tokens, "max_output_tokens")
    if type(seed) is not int or not isinstance(model, str) or not model.strip():
        raise ValueError("Dedupe requires integer seed and explicit model")
    if type(retry_paid_error) is not bool:
        raise ValueError("retry_paid_error must be a boolean")
    incoming = checked_ideas(ideas, exam_id)
    path = stage_path(run_dir, exam_id, replay)
    with recording_lock(path / "dedupe"):
        state = load_state(path, exam_id, ledger, {"backend": backend.name, "model": model})
        known = dict(state["ideas"])
        if state["pending"]:
            known.update({row["idea_id"]: row for row in state["requests"][state["pending"]]["ideas"]})
        for key, row in incoming.items():
            if key in known and known[key] != row:
                raise ValueError(f"Conflicting content for idea {key}")
        # One call identity cannot change lineage across configurations or repetitions.
        lineage = {}
        for row in [*known.values(), *incoming.values()]:
            identity = [row[field] for field in ("exam_id", "config", "rep", "agent", "batch", "level", "model")]
            if row["call_key"] in lineage and lineage[row["call_key"]] != identity:
                raise ValueError("Conflicting call_key lineage across input units")
            lineage[row["call_key"]] = identity
        original_ids = {row["mechanism_id"] for row in state["table"]["mechanisms"]}
        # The initial state must exist before any derived projection can survive a crash.
        save_state(path, state)
        export_outputs(path, state)
        while state["pending"] or any(key not in state["ideas"] for key in incoming):
            if state["pending"]:
                request = state["requests"][state["pending"]]
                if (request["model"], request["backend"], request["max_output_tokens"]) != (model, backend.name, max_output_tokens):
                    raise ValueError("Pending dedupe model, backend, or token identity conflict")
            else:
                batch = [row for key, row in incoming.items() if key not in state["ideas"]][:batch_size]
                prompt = mechanism_prompt(batch, state["table"], state["ideas"])
                key = "dedupe_" + digest([str(path), "authoritative" if replay is None else "replay", exam_id,
                                          prompt, model, backend.name, max_output_tokens, seed, 1])
                request = {"call_key": key, "exam_id": exam_id, "config": "dedupe", "rep": 1, "agent": 1,
                           "level": "round1", "purpose": "dedupe", "backend": backend.name, "model": model,
                           "attempt": 1, "started_at": None, "duration_ms": None, "ideas": batch,
                           "prompt": prompt, "max_output_tokens": max_output_tokens, "seed": seed}
                state["requests"][key] = request
                state["pending"] = key
                save_state(path, state)
            key = state["pending"]
            events = [row for row in read_rows(ledger.path) if row["call_key"] == key]
            already_failed = bool(events and events[-1]["event"] == "settle" and events[-1].get("outcome") == "paid_error")
            measured = MeasuredBackend(backend, request, lambda: save_state(path, state))
            print(f"Dedupe {exam_id} mode={'authority' if replay is None else 'replay'} "
                  f"committed={len(state['ideas'])} batch={len(request['ideas'])} model={model}", file=sys.stderr)
            try:
                result = guarded_call(ledger, measured, key, request["prompt"], model, max_output_tokens)
            except (LedgerError, NotStarted) as exc:
                sync_call(state, key, ledger)
                failure = {"stage": "dedupe", "call_key": key, "reason": type(exc).__name__, "detail": str(exc)}
                if failure not in state["failures"]:
                    state["failures"].append(failure)
                save_state(path, state)
                export_outputs(path, state)
                # A fresh paid failure always stops. Only explicit resume may spend a new attempt.
                if isinstance(exc, PaidCallError) and already_failed and retry_paid_error:
                    if request["attempt"] >= MAX_PAID_ATTEMPTS:
                        raise PaidCallError(f"Dedupe paid retry limit reached: {MAX_PAID_ATTEMPTS} attempts; "
                                            "costs retained, no further calls issued") from exc
                    retry = deepcopy(request)
                    retry["attempt"] += 1
                    retry["call_key"] = "dedupe_" + digest([key, retry["attempt"]])
                    retry.update(started_at=None, duration_ms=None)
                    retry.pop("receipt", None)
                    request.pop("prompt", None)
                    state["requests"][retry["call_key"]] = retry
                    state["pending"] = retry["call_key"]
                    save_state(path, state)
                    continue
                raise
            if measured.persistence_error is not None:
                raise measured.persistence_error
            sync_call(state, key, ledger)
            buckets, fallback, errors = parse_partition(result.text, request["ideas"], state["table"])
            previous = deepcopy(state["table"])
            table = state["table"]
            index = {row["mechanism_id"]: row for row in table["mechanisms"]}
            next_number = max((int(key[2:]) for key in index), default=0)
            state["ideas"].update({row["idea_id"]: row for row in request["ideas"]})
            rng = random.Random(request["seed"])
            for bucket in buckets:
                target = bucket["target"]
                if target in index:
                    row = index[target]
                    row["idea_ids"].extend(bucket["idea_ids"])
                else:
                    next_number += 1
                    row = {"mechanism_id": f"m-{next_number:03d}", "label": bucket["label"],
                           "idea_ids": bucket["idea_ids"], "representative": rng.choice(sorted(bucket["idea_ids"])),
                           "in_baseline": False}
                    table["mechanisms"].append(row)
                row["in_baseline"] = any(state["ideas"][item]["level"] == "baseline" for item in row["idea_ids"])
            state["fallback_ids"].extend(fallback)
            state["failures"].extend({"stage": "dedupe", "call_key": key, "reason": "invalid_partition", "detail": error}
                                     for error in errors)
            validate_table(table, state["ideas"], exam_id)
            state["previous_table"] = previous
            state["pending"] = None
            request.pop("prompt", None)
            save_state(path, state)
            export_outputs(path, state)
        return {"table": deepcopy(state["table"]), "authoritative": replay is None,
                "new_mechanism_ids": [row["mechanism_id"] for row in state["table"]["mechanisms"]
                                      if row["mechanism_id"] not in original_ids],
                "fallback_ids": list(state["fallback_ids"]), "failures": read_rows(path / "dedupe/failures.jsonl"),
                "mechanisms_path": str(path / "mechanisms.json"), "calls_path": str(path / "dedupe/calls.jsonl"),
                "failures_path": str(path / "dedupe/failures.jsonl")}


def abandon_pending(run_dir, exam_id, ledger, key):
    """Free the queue from a paid failure whose unit was replaced. Its ideas stay on disk, ungrouped; the charge stays."""
    path = stage_path(run_dir, exam_id, None)
    with recording_lock(path / "dedupe"):
        classifier = read_file(path / "dedupe/state.json")["state"]["classifier"]
        state = load_state(path, exam_id, ledger, classifier)
        if state["pending"] != key:
            return
        sync_call(state, key, ledger)
        state.setdefault("abandoned", []).append(key)
        state["pending"] = None
        failure = {"stage": "dedupe", "call_key": key, "reason": "abandoned", "detail": "Unit replaced; these ideas were never grouped"}
        if failure not in state["failures"]:
            state["failures"].append(failure)
        save_state(path, state)
        export_outputs(path, state)


def collisions(table, reference_ids, batch_ids):
    """Return only batch members sharing a mechanism with the explicit reference."""
    reference = set(reference_ids)
    membership = {key: row["mechanism_id"] for row in table["mechanisms"] for key in row["idea_ids"]}
    if not (reference | set(batch_ids)) <= membership.keys():
        raise ValueError("Collision inputs contain IDs outside the mechanism partition")
    seen = {membership[key] for key in reference}
    return [key for key in dict.fromkeys(batch_ids) if key not in reference and membership[key] in seen]


def differences(groups, human_groups, ids):
    machine, human = partition_labels(groups, ids), partition_labels(human_groups, ids)
    result = {"false_merges": [], "false_splits": []}
    # ceiling: exhaustive pair evidence is quadratic; use a streamed report when exam size demands it.
    for i, j in combinations(range(len(ids)), 2):
        same_machine, same_human = machine[i] == machine[j], human[i] == human[j]
        if same_machine != same_human:
            kind = "false_merges" if same_machine else "false_splits"
            result[kind].append({"idea_ids": [ids[i], ids[j]],
                                 "machine_groups": [machine[i], machine[j]],
                                 "human_groups": [human[i], human[j]]})
    return result


def calibrate(ideas, human, run_dir, *, exam_id, seed, backend, ledger, model_a, model_b,
              allowed_models, name, batch_size=40, max_output_tokens=2048, retry_paid_error=False):
    """Compare two explicit cold-start replay partitions against the same human universe."""
    ideas = list(checked_ideas(ideas, exam_id).values())
    ids = [row["idea_id"] for row in ideas]
    if not ids:
        raise ValueError("Calibration requires a nonempty idea universe")
    if (not isinstance(human, dict) or set(human) != {"exam_id", "groups"}
            or human["exam_id"] != exam_id):
        raise ValueError("Human groups must name the same exam and contain groups")
    human_labels = partition_labels(human["groups"], ids)
    if (not isinstance(model_a, str) or not model_a.strip() or not isinstance(model_b, str) or not model_b.strip()
            or model_a == model_b or model_a not in allowed_models or model_b not in allowed_models):
        raise ValueError("Calibration requires two explicit distinct models from configured models")
    positive_int(batch_size, "batch_size")
    positive_int(max_output_tokens, "max_output_tokens")
    if type(seed) is not int:
        raise ValueError("Calibration requires an integer seed")
    safe_segment(name)
    directory = stage_path(run_dir, exam_id, name).parent
    for suffix in ("__a", "__b"):
        stage_path(run_dir, exam_id, name + suffix)
    identity = {"ideas": ideas, "human": human, "seed": seed, "models": [model_a, model_b],
                "backend": backend.name, "batch_size": batch_size, "max_output_tokens": max_output_tokens,
                "ledger": str(ledger.path)}
    with recording_lock(directory):
        identity_path = directory / "calibration_input.json"
        if identity_path.exists() and read_file(identity_path) != identity:
            raise ValueError("Calibration replay name has a conflicting input identity")
        if not identity_path.exists() and any(stage_path(run_dir, exam_id, name + suffix).exists()
                                              for suffix in ("__a", "__b")):
            raise ValueError("Calibration requires cold replay partitions, not prepopulated outputs")
        atomic_write(identity_path, dumps(identity) + "\n")
        report = {"exam_id": exam_id, "authoritative": False, "status": "incomplete",
                  "models": {"a": model_a, "b": model_b}, "ami": {"a_human": None, "b_human": None, "a_b": None},
                  "groups": {"human": human["groups"]},
                  "ideas": [{field: row[field] for field in ("idea_id", "title", "text")} for row in ideas],
                  "differences": {}, "difference_counts": {}, "fallback_counts": {}, "outputs": {}, "warnings": []}
        report_path = directory / "calibration.json"
        atomic_write(report_path, dumps(report) + "\n")
        partitions = {}
        for key, model in (("a", model_a), ("b", model_b)):
            try:
                result = deduplicate(ideas, run_dir, exam_id=exam_id, seed=seed, backend=backend,
                                     ledger=ledger, model=model, batch_size=batch_size,
                                     max_output_tokens=max_output_tokens, replay=name + "__" + key,
                                     retry_paid_error=retry_paid_error)
            except (LedgerError, NotStarted) as exc:
                report["error"] = f"{type(exc).__name__}: {exc}"
                atomic_write(report_path, dumps(report) + "\n")
                raise
            groups = [row["idea_ids"] for row in result["table"]["mechanisms"]]
            partitions[key] = partition_labels(groups, ids)
            report["groups"][key] = groups
            report["ami"][key + "_human"] = adjusted_mutual_info(partitions[key], human_labels)
            report["differences"][key] = differences(groups, human["groups"], ids)
            report["difference_counts"][key] = {kind: len(pairs) for kind, pairs in report["differences"][key].items()}
            report["fallback_counts"][key] = len(result["fallback_ids"])
            report["outputs"][key] = {field: result[field] for field in ("mechanisms_path", "calls_path", "failures_path")}
            if result["failures"]:
                report["warnings"].append({"model": model, "failures": result["failures"], "fallback_ids": result["fallback_ids"]})
            atomic_write(report_path, dumps(report) + "\n")
        report["ami"]["a_b"] = adjusted_mutual_info(partitions["a"], partitions["b"])
        report["status"] = "complete_with_fallback" if report["warnings"] else "complete"
        atomic_write(report_path, dumps(report) + "\n")
        return report


def fake_partition(prompt, model, max_output_tokens):
    """Local wiring fixture: match identical text only, not a semantic evaluator."""
    existing_text, groups = {}, {}
    # The existing data block ends at the trusted New ideas header.
    old_prompt, new_prompt = prompt.split("\nNew ideas:\n", 1)
    lines = old_prompt.split("Existing mechanisms:\n", 1)[1].split("\n")
    existing = json.loads("\n".join(lines[2:-1]))
    lines = new_prompt.split("\n")
    new = json.loads("\n".join(lines[2:-1]))
    for row in existing:
        for variant in row["variants"]:
            existing_text[variant["text"]] = row["mechanism_id"]
    for row in new:
        target = existing_text.get(row["text"])
        if target is None:
            target = "new:" + digest(row["text"])
        groups.setdefault(target, {"target": target, "label": row["title"], "idea_ids": []})["idea_ids"].append(row["idea_id"])
    return dumps({"groups": list(groups.values())})


def run_cli(args):
    config = load_config(args.config)
    backend = make_backend(config)
    if config["backend"]["kind"] == "fake" and "responses" not in config["backend"]:
        backend.responses = fake_partition
    ledger = BudgetLedger(args.ledger or default_ledger_path(backend.name, args.run_dir),
                          args.limit if args.limit is not None else config["budget_usd"])
    if not args.ideas.is_file():
        raise ValueError("Idea input file does not exist")
    ideas = read_rows(args.ideas)
    options = {"exam_id": args.exam_id, "seed": args.seed, "backend": backend, "ledger": ledger,
               "retry_paid_error": args.retry_paid_error,
               "batch_size": args.batch_size, "max_output_tokens": (args.max_output_tokens
                   if args.max_output_tokens is not None else config["max_output_tokens"])}
    try:
        if args.action == "calibrate":
            result = calibrate(ideas, read_file(args.human_groups), args.run_dir, **options,
                               model_a=args.model_a, model_b=args.model_b, allowed_models=config["models"], name=args.name)
        else:
            model = args.model if args.model is not None else config["default_model"]
            if model not in config["models"]:
                raise ValueError("Dedupe model must be in configured models")
            result = deduplicate(ideas, args.run_dir, **options, model=model, replay=args.replay)
    except NotStarted as exc:
        raise ValueError(f"Dedupe stopped before provider startup: {exc}") from exc
    print(dumps(result))


def register(subparsers):
    parser = subparsers.add_parser("dedupe", help="Incremental mechanisms and human grouping calibration")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("run", "calibrate"):
        command = actions.add_parser(action)
        command.add_argument("--ideas", type=Path, required=True)
        command.add_argument("--exam-id", required=True)
        command.add_argument("--run-dir", type=Path, required=True)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--seed", type=int, required=True)
        command.add_argument("--ledger", type=Path)
        command.add_argument("--limit")
        command.add_argument("--batch-size", type=int, default=40)
        command.add_argument("--max-output-tokens", type=int)
        command.add_argument("--retry-paid-error", action="store_true",
                             help="Explicitly resume a settled paid failure, at most two total attempts")
        if action == "calibrate":
            command.add_argument("--human-groups", type=Path, required=True)
            command.add_argument("--model-a", required=True)
            command.add_argument("--model-b", required=True)
            command.add_argument("--name", required=True)
        else:
            command.add_argument("--model")
            command.add_argument("--replay")
        command.set_defaults(func=lambda args: cli_boundary(run_cli, args))
