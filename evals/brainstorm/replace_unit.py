"""Replace one failed unit with one new repetition, only when the author asks. Nothing paid is erased."""

from datetime import datetime, timezone
from pathlib import Path

from . import dedupe, judge, loop, record, replacements
from .backends import NotStarted, amount, exact_sum
from .budget import BudgetLedger, LedgerError, cli_boundary, default_ledger_path, dumps
from .config import load_config, positive_int
from .exam import load_exams


class ReplacementFailed(ValueError):
    pass


def last_events(path):
    return {row["call_key"]: row for row in record.read_rows(path)}


def paid_failure(event):
    return bool(event) and event["event"] == "settle" and event.get("outcome") == "paid_error"


def unreconciled(event):
    return bool(event) and event["event"] in ("reserve", "uncertain")


def signed_state(path):
    envelope = record.read_file(path)
    if not isinstance(envelope, dict) or "state" not in envelope or envelope.get("digest") != record.digest(envelope["state"]):
        raise ValueError(f"Invalid state digest: {path.name}")
    return envelope["state"]


def unit_path(root, exam_id, config, rep):
    return Path(root).resolve() / exam_id / config / f"rep-{rep}"


def unit_status(root, exam_id, config, rep, events):
    """Read-only. `failed` means no later run can finish this unit; a budget stop or an unstarted call can."""
    path = unit_path(root, exam_id, config, rep)
    if not (path / "loop_state.json").is_file():
        return {"state": "absent", "stage": None, "cause": None, "call_keys": []}
    state = signed_state(path / "loop_state.json")
    failures, unsettled = [], []

    def classify(stage, key, cause="paid_error"):
        event = events.get(key)
        if unreconciled(event):
            unsettled.append(key)
        elif paid_failure(event):
            failures.append((stage, cause, key))

    if (path / "record_state.json").is_file():
        for key, request in record.load_state(path, None)["requests"].items():
            if request["status"] == "paid_error" or (request["status"] == "parse_error" and request["attempt"] == 2):
                failures.append(("recording", request["status"], key))
            elif request["status"] == "pending":
                classify("recording", key)
    shared = Path(root).resolve() / exam_id / "dedupe/state.json"
    if shared.is_file():
        grouping = signed_state(shared)
        pending = grouping["requests"][grouping["pending"]] if grouping["pending"] else None
        owners = {(row["config"], row["rep"]) for row in pending["ideas"]} if pending else set()
        if (config, rep) in owners:
            # A batch from the hand-run grouping command can mix units; dropping it would also take other units' ideas.
            if len(owners) > 1:
                unsettled.append(grouping["pending"])
            else:
                classify("grouping", grouping["pending"])
    batches = state["batches"]
    if batches and batches[-1]["stage"] != "done" and batches[-1]["fallback_ids"]:
        failures.append(("grouping", "invalid_partition", None))
    constraint = path / "judges/constraint/state.json"
    if constraint.is_file():
        judged = judge.read_state(constraint)
        key = next(iter(judged["requests"]))
        if judged.get("invalid_response"):
            failures.append(("constraint", "invalid_response", key))
        elif judged.get("result") is None:
            classify("constraint", key)
    if unsettled:
        return {"state": "open", "stage": None, "cause": None, "call_keys": unsettled}
    if failures:
        stage, cause, _ = failures[0]
        return {"state": "failed", "stage": stage, "cause": cause, "call_keys": [row[2] for row in failures if row[2]]}
    return {"state": "complete" if state["complete"] else "resumable", "stage": None, "cause": None, "call_keys": []}


def settle_failed_unit(root, exam, config, rep, ledger):
    """Bring receipts up to the ledger and free the exam's grouping queue. Safe to repeat; never touches the ledger."""
    exam_id = exam["id"]
    events = last_events(ledger.path)
    path = unit_path(root, exam_id, config, rep)
    if (path / "record_state.json").is_file():
        with record.recording_lock(path):
            state = record.load_state(path, None)
            changed = False
            for key, request in state["requests"].items():
                if request["status"] == "pending" and paid_failure(events.get(key)):
                    record.sync_call(state, key, ledger)
                    request["status"] = "paid_error"
                    record.add_failure(state, "generate", key, "paid_error", "Reconciled by hand after a crash; the call cannot be retried")
                    changed = True
            if changed:
                record.export_outputs(path, state, exam)
    shared = Path(root).resolve() / exam_id / "dedupe/state.json"
    if shared.is_file():
        grouping = signed_state(shared)
        pending = grouping["requests"][grouping["pending"]] if grouping["pending"] else None
        if pending and {(row["config"], row["rep"]) for row in pending["ideas"]} == {(config, rep)} and paid_failure(events.get(grouping["pending"])):
            dedupe.abandon_pending(root, exam_id, ledger, grouping["pending"])
    if (path / "judges/constraint/state.json").is_file():
        judge.sync_failed(path / "judges/constraint", ledger)


def receipts_usd(root, row, rep, *, shared_keys=()):
    """Everything this unit's own files recorded, plus any grouping call that was its alone."""
    path = unit_path(root, row["exam_id"], row["config"], rep)
    amounts = [amount(entry["usd"]) for file in sorted(path.rglob("calls.jsonl")) for entry in record.read_rows(file)]
    shared = Path(root).resolve() / row["exam_id"] / "dedupe/calls.jsonl"
    amounts += [amount(entry["usd"]) for entry in record.read_rows(shared) if entry["call_key"] in shared_keys]
    return exact_sum(0, *amounts)


def describe(root, row, events):
    """What a reader needs to know about one replacement, from the files as they are now."""
    keys = set(row["call_keys"]) if row["stage"] == "grouping" else set()
    state = unit_status(root, row["exam_id"], row["config"], row["new_rep"], events)["state"]
    return {key: row[key] for key in ("exam_id", "config", "failed_rep", "new_rep", "stage", "cause", "ungrouped_ideas", "reason")} | {
        "failed_usd": receipts_usd(root, row, row["failed_rep"], shared_keys=keys),
        "new_usd": receipts_usd(root, row, row["new_rep"]),
        "status": {"complete": "complete", "failed": "failed"}.get(state, "unfinished")}


def identity_differences(recorded, expected):
    return sorted(key for key in set(recorded) | set(expected) if recorded.get(key) != expected.get(key))


def grouped_ids(root, exam_id):
    path = Path(root).resolve() / exam_id / "mechanisms.json"
    if not path.is_file():
        return set()
    return {key for row in record.read_file(path)["mechanisms"] for key in row["idea_ids"]}


def replace_unit(exam, config, run_dir, *, failed_rep, new_rep, backend, ledger, dedupe_config, dedupe_backend, reason=None):
    positive_int(failed_rep, "failed_rep")
    positive_int(new_rep, "new_rep")
    if failed_rep == new_rep:
        raise ValueError("The new repetition cannot be the failed repetition itself")
    root, exam_id, name = Path(run_dir).resolve(), exam["id"], config["name"]
    failed_path = unit_path(root, exam_id, name, failed_rep)
    if not (failed_path / "loop_state.json").is_file():
        raise ValueError(f"{exam_id}/{name} rep {failed_rep} has no recorded loop; only a failed loop unit can be replaced")
    # The seed comes from the failed unit, so there is no way to pick a kinder one.
    recorded = signed_state(failed_path / "loop_state.json")["identity"]
    seed = recorded["seed"]
    expected = loop.loop_identity(exam, config, seed, failed_rep, ledger, backend, dedupe_config, dedupe_backend)
    changed = identity_differences(recorded, expected)
    if changed:
        raise ValueError(f"A replacement keeps the failed unit's setup; these differ: {', '.join(changed)}. "
                         "Changing the ledger, config or classifier cannot erase a failure")
    new_path = unit_path(root, exam_id, name, new_rep)
    # Reconciles a dead owner's reservation into its honest state before anything below reads the ledger.
    ledger.status()
    with record.recording_lock(root / exam_id):
        rows = replacements.load(root, exam_id)
        row = next((item for item in rows if item["config"] == name and item["failed_rep"] == failed_rep), None)
        if row is None:
            if failed_rep in replacements.new_reps(rows, name):
                raise ValueError(f"{exam_id}/{name} rep {failed_rep} is itself a replacement and cannot be replaced. Stop and report")
            events = last_events(ledger.path)
            status = unit_status(root, exam_id, name, failed_rep, events)
            if status["state"] == "open":
                raise ValueError(f"Reconcile these calls first with ledger settle or ledger release: {', '.join(status['call_keys'])}")
            if status["state"] != "failed":
                raise ValueError(f"{exam_id}/{name} rep {failed_rep} did not fail ({status['state']}). A unit stopped by the limit "
                                 "or by another unit's grouping call can resume; only a failed unit can be replaced")
            if new_path.exists() and any(new_path.iterdir()) or new_rep in replacements.retired_reps(rows, name):
                raise ValueError(f"{exam_id}/{name} rep {new_rep} already exists; a replacement needs a new repetition")
            stored = record.read_rows(failed_path / "ideas.jsonl")
            row = {"exam_id": exam_id, "config": name, "failed_rep": failed_rep, "new_rep": new_rep, "stage": status["stage"],
                   "cause": status["cause"], "call_keys": status["call_keys"], "reason": reason,
                   "ungrouped_ideas": len({entry["idea_id"] for entry in stored} - grouped_ids(root, exam_id)),
                   "ts": datetime.now(timezone.utc).isoformat()}
            new_path.mkdir(parents=True, exist_ok=True)
            replacements.append(root, exam_id, row)
        elif row["new_rep"] != new_rep:
            raise ValueError(f"{exam_id}/{name} rep {failed_rep} was already replaced by rep {row['new_rep']}; "
                             "each failed unit is replaced once")
        settle_failed_unit(root, exam, name, failed_rep, ledger)
    return run_new_unit(exam, config, root, row, seed=seed, backend=backend, ledger=ledger,
                        dedupe_config=dedupe_config, dedupe_backend=dedupe_backend)


def run_new_unit(exam, config, root, row, *, seed, backend, ledger, dedupe_config, dedupe_backend):
    exam_id, name, new_rep = exam["id"], config["name"], row["new_rep"]

    def stopped(status):
        settle_failed_unit(root, exam, name, new_rep, ledger)
        return ReplacementFailed(f"Replacement rep {new_rep} of {exam_id}/{name} also failed at {status['stage']}. It cannot be replaced: "
                                 f"stop and report. Spent {receipts_usd(root, row, row['failed_rep'])} on the failed unit and "
                                 f"{receipts_usd(root, row, new_rep)} on its replacement; both stay on the ledger")

    status = unit_status(root, exam_id, name, new_rep, last_events(ledger.path))
    if status["state"] == "failed":
        raise stopped(status)
    if status["state"] == "open":
        raise ValueError(f"Reconcile these calls first with ledger settle or ledger release: {', '.join(status['call_keys'])}")
    if status["state"] != "complete":
        try:
            loop.run_loop(exam, config, root, seed=seed, backend=backend, ledger=ledger, dedupe_config=dedupe_config,
                          dedupe_backend=dedupe_backend, rep=new_rep)
        except (ValueError, LedgerError) as exc:
            status = unit_status(root, exam_id, name, new_rep, last_events(ledger.path))
            if status["state"] == "failed":
                raise stopped(status) from exc
            raise
    return describe(root, row, last_events(ledger.path))


def run_cli(args):
    config, classifier = load_config(args.config), load_config(args.dedupe_config)
    if args.run_dir.resolve().is_relative_to(args.exam_dir.resolve()):
        raise ValueError("run-dir must not be inside the read-only exam-dir")
    exam = next((row for row in load_exams(args.exam_dir) if row["id"] == args.exam_id), None)
    if exam is None:
        raise ValueError("exam-id was not found")
    backend, grouping = record.make_backend(config), record.make_backend(classifier)
    if classifier["backend"]["kind"] == "fake" and "responses" not in classifier["backend"]:
        grouping.responses = dedupe.fake_partition
    ledger = BudgetLedger(args.ledger or default_ledger_path(backend.name, args.run_dir), args.limit or config["budget_usd"])
    try:
        result = replace_unit(exam, config, args.run_dir, failed_rep=args.failed_rep, new_rep=args.new_rep, backend=backend,
                              ledger=ledger, dedupe_config=classifier, dedupe_backend=grouping, reason=args.reason)
    except NotStarted as exc:
        raise ValueError(f"Replacement stopped before provider startup: {exc}") from exc
    print(dumps(result))


def register(subparsers):
    parser = subparsers.add_parser("replace-unit", help="Replace one failed unit with one new repetition, once, and keep the record")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--exam-dir", type=Path, required=True)
    parser.add_argument("--exam-id", required=True)
    parser.add_argument("--config", type=Path, required=True, help="The failed unit's config, unchanged")
    parser.add_argument("--dedupe-config", type=Path, required=True, help="The failed unit's classifier config, unchanged")
    parser.add_argument("--failed-rep", type=int, required=True)
    parser.add_argument("--new-rep", type=int, required=True)
    parser.add_argument("--ledger", type=Path, help="Must be the ledger the failed unit used")
    parser.add_argument("--limit")
    parser.add_argument("--reason", help="Kept in the replacement record")
    parser.set_defaults(func=lambda args: cli_boundary(run_cli, args))
