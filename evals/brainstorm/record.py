"""Budgeted single-batch recording and recoverable local materialized outputs."""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import re
import subprocess
from pathlib import Path
import sys
import time

from .allocate import allocate
from .backends import CallResult, ClaudeCliBackend, CommandBackend, FakeBackend, NotStarted, amount, exact_sum, token_counts
from .budget import (BudgetLedger, BudgetExceeded, LedgerError, PaidCallError, atomic_write,
                     cli_boundary, default_ledger_path, dumps, guarded_call)
from .config import LEVELS, load_config, positive_int
from .exam import load_exams, read_json, read_jsonl, safe_segment, text_list
from .prompts import generation_prompt, model_json
from . import replacements


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def read_file(path):
    return read_json(path, parse_float=Decimal)


def read_rows(path):
    path = Path(path)
    return read_jsonl(path) if path.exists() else []


def output_dir(run_dir, exam_id, config, rep):
    positive_int(rep, "rep")
    root = Path(run_dir).resolve()
    path = root / safe_segment(exam_id) / safe_segment(config) / f"rep-{rep}"
    if not path.resolve().is_relative_to(root):
        raise ValueError("Output path escapes run directory")
    repo = Path(__file__).resolve().parents[2]
    if path.is_relative_to(repo):
        check = subprocess.run(["git", "check-ignore", "--quiet", "--no-index", str(path.relative_to(repo))],
                               cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if check.returncode:
            print("Privacy warning: output directory is not ignored by git; private answers may be committed.", file=sys.stderr)
    return path


@contextmanager
def recording_lock(path):
    path.mkdir(parents=True, exist_ok=True)
    with (path / "record.lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def save_state(path, state):
    atomic_write(path / "record_state.json", dumps({"digest": digest(state), "state": state}) + "\n")


def load_state(path, identity):
    state_path = path / "record_state.json"
    if state_path.exists():
        envelope = read_file(state_path)
        if (not isinstance(envelope, dict) or "state" not in envelope
                or envelope.get("digest") != digest(envelope["state"])):
            raise ValueError("record_state: invalid digest")
        state = envelope["state"]
        if identity is not None and state["identity"] != identity:
            raise ValueError("Recording identity conflict: config, seed, exam or ledger changed")
    else:
        if any((path / name).exists() for name in ("ideas.jsonl", "calls.jsonl", "failures.jsonl", "run_summary.json")):
            raise ValueError("Missing record_state with surviving outputs")
        state = {"identity": identity, "batches": {}, "requests": {}, "ideas": [], "calls": [], "failures": []}
    for name, key in (("ideas", "idea_id"), ("calls", "call_key")):
        expected = {row[key]: row for row in state[name]}
        seen = set()
        for row in read_rows(path / f"{name}.jsonl"):
            retired = name == "calls" and (row in state.get("released_call_history", [])
                                          or row in state.get("call_projection_history", []))
            if row.get(key) in seen or (expected.get(row.get(key)) != row and not retired):
                raise ValueError(f"{name}: conflicting or corrupt persisted row")
            seen.add(row[key])
    for row in read_rows(path / "failures.jsonl"):
        if row not in state["failures"]:
            raise ValueError("failures: conflicting persisted row")
    if (path / "run_summary.json").exists():
        if not isinstance(read_file(path / "run_summary.json"), dict):
            raise ValueError("run_summary: expected an object")
    return state


def add_failure(state, stage, key, reason, detail):
    row = {"stage": stage, "call_key": key, "reason": reason, "detail": detail}
    if not any((f["stage"], f["call_key"], f["reason"]) == (stage, key, reason) for f in state["failures"]):
        state["failures"].append(row)


def active_failures(state):
    transient = {"BudgetExceeded", "NotStarted", "CallUncertain", "CallBusy"}
    return [row for row in state["failures"]
            if (row["reason"] not in transient
                or state["requests"].get(row["call_key"], {}).get("status", "pending") == "pending")
            and (row["reason"] != "invalid_output"
                 or state["requests"][row["call_key"]]["attempt"] == 2)]


def scan_response(text, markers):
    if not isinstance(text, str) or not text.strip():
        return False, False
    stack = [text]
    complete = True
    try:
        # Pairs retain every key and value, including overwritten duplicate keys.
        stack.append(json.loads(text, object_pairs_hook=list))
    except (ValueError, RecursionError):
        complete = False
    found = False
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            found |= any(marker in value for marker in markers)
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
    return found, complete


def summarize(state, exam):
    exam = state.get("guard_exam", exam)
    calls = state["calls"]
    tokens = {key: sum(call["tokens"].get(key, 0) for call in calls)
              for key in ("input", "cache_write", "cache_read", "output")}
    tokens["total"] = tokens["input"] + tokens["cache_write"] + tokens["output"]
    complete = bool(state["batches"]) and all(batch["complete"] for batch in state["batches"].values())
    probes = [probe for probe in exam["planted"] if probe["kind"] == "injection"]
    markers = [probe["marker"] for probe in probes]
    scanned, delivered = {}, set()
    known = set(state.get("injection_violations", []))
    called = {call["call_key"] for call in calls}
    external = bool(state.get("import_receipts") or state.get("identity", {}).get("import"))
    for key, request in state["requests"].items():
        source = request.get("call_key", key)
        receipt = request.get("receipt") or {}
        found, full_scan = scan_response(receipt.get("text"), markers)
        if found:
            known.add(source)
        scanned[source] = scanned.get(source, True) and full_scan
        external |= request.get("external", False)
        if not request.get("external") and source in called:
            delivered.update(request.get("cards", []))
    for idea in state["ideas"]:
        output = {key: idea[key] for key in ("title", "text", "cards_used", "decomposition")}
        if scan_response(dumps(output), markers)[0]:
            known.add(idea["call_key"])
    state["injection_violations"] = sorted(known)
    injection = {"status": "not_evaluated", "violations": len(known) if known else None}
    if (complete and not external and probes and called
            and all(probe["card_id"] in delivered for probe in probes)
            and all(scanned.get(key, False) for key in called)):
        injection = {"status": "checked", "violations": len(known)}
    elif not probes and not external and not known:
        injection = {"status": "not_applicable", "violations": None}
    return {"calls": len(calls), "usd": exact_sum(0, *(call["usd"] for call in calls)),
            "usd_estimated": exact_sum(0, *(call["usd"] for call in calls if call["usd_estimated"])),
            "tokens": tokens, "ideas": len(state["ideas"]), "failures": len(active_failures(state)),
            "measurements": {"duration_missing": sum(c["duration_ms"] is None for c in calls),
                             "started_at_missing": sum(c["started_at"] is None for c in calls),
                             "tokens_missing": sum(set(c["tokens"]) != {"input", "cache_write", "cache_read", "output"} for c in calls)},
            "complete": complete, "allocations": {k: v["assignments"] for k, v in state["batches"].items()},
            "guards": {"injection": injection, "constraint": {
                "status": "not_evaluated" if exam["constraints"] else "not_applicable", "violations": None}}}


def export_outputs(path, state, exam):
    # ceiling: atomic whole-unit rewrites for small exam batches; index when IO dominates calls.
    summary = summarize(state, exam)
    # Positive evidence is monotonic and durable even if later coverage is lost.
    save_state(path, state)
    for name in ("ideas", "calls", "failures"):
        rows = active_failures(state) if name == "failures" else state[name]
        atomic_write(path / f"{name}.jsonl", "".join(dumps(row) + "\n" for row in rows))
    atomic_write(path / "run_summary.json", dumps(summary) + "\n")
    return summary


def parse_ideas(text, cards, originals=None):
    """originals=N reads a bundled collision return: each idea names the 1..N original it replaces, at most once."""
    fields = {"title", "text", "cards_used", "decomposition"} | ({"replaces"} if originals is not None else set())
    try:
        payload = json.loads(model_json(text))
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"Invalid generated JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"ideas"} or not isinstance(payload["ideas"], list) or not payload["ideas"]:
        raise ValueError("Generated response must contain a nonempty ideas array")
    for index, row in enumerate(payload["ideas"]):
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError(f"Idea {index}: expected only {'/'.join(sorted(fields))}")
        if originals is not None and (type(row["replaces"]) is not int or not 1 <= row["replaces"] <= originals):
            raise ValueError(f"Idea {index}: replaces must be an original number from 1 to {originals}")
        for field in ("title", "text"):
            if not isinstance(row[field], str) or not row[field].strip():
                raise ValueError(f"Idea {index}: missing {field}")
        text_list(row["cards_used"], "cards_used")
        if len(set(row["cards_used"])) != len(row["cards_used"]) or not set(row["cards_used"]) <= set(cards):
            raise ValueError(f"Idea {index}: cards_used contains unassigned or duplicate cards")
        if row["decomposition"] is not None:
            text_list(row["decomposition"], "decomposition")
    if originals is not None and len({row["replaces"] for row in payload["ideas"]}) != len(payload["ideas"]):
        raise ValueError("Two replacements name the same original")
    return payload["ideas"]


class MeasuredBackend:
    """Durable response measurement before the ledger commits its successful cache."""

    def __init__(self, backend, request, persist):
        self.backend, self.request, self.persist = backend, request, persist
        self.name = backend.name
        self.persistence_error = None

    def estimate_max_usd(self, *args):
        return self.backend.estimate_max_usd(*args)

    def call(self, *args):
        self.request["started_at"] = datetime.now(timezone.utc).isoformat()
        self.persist()
        start = time.monotonic()
        try:
            result = self.backend.call(*args)
        except Exception:
            self.request["duration_ms"] = round((time.monotonic() - start) * 1000)
            self.persist()
            raise
        self.request["duration_ms"] = round((time.monotonic() - start) * 1000)
        self.request["receipt"] = asdict(result)
        try:
            self.persist()
        except OSError as exc:
            # Known billing must reach the ledger even when this recording disk fails.
            self.persistence_error = exc
        return result


def sync_call(state, key, ledger):
    # ceiling: one ledger scan per response; add a public indexed receipt API for large batches.
    events = [row for row in read_rows(ledger.path) if row["call_key"] == key]
    if not events:
        return
    if events[-1]["event"] == "release":
        prior = [call for call in state["calls"] if call["call_key"] == key]
        state.setdefault("released_call_history", []).extend(prior)
        state["calls"] = [call for call in state["calls"] if call["call_key"] != key]
        return
    event = events[-1]
    request = state["requests"][key]
    settled = event["event"] == "settle"
    # Null is explicit lost measurement, not a synthetic zero or a reason to lose billing.
    row = {field: request[field] for field in ("call_key", "exam_id", "config", "rep", "agent", "level",
                                               "purpose", "backend", "model", "attempt", "started_at", "duration_ms")}
    row.update(tokens=event["tokens"], usd=event["usd"],
               usd_estimated=event["estimated"] if settled else True,
               outcome=event.get("outcome", "uncertain"))
    state.setdefault("call_projection_history", []).extend(
        call for call in state["calls"] if call["call_key"] == key and call != row)
    state["calls"] = [call for call in state["calls"] if call["call_key"] != key] + [row]


def checked_assignments(rows, exam, config, level, returned_from):
    if not isinstance(rows, list) or not rows:
        raise ValueError("assignments must be a nonempty array")
    agents = []
    cards = {card["id"] for card in exam["card_data"]}
    count = (len(cards) if config["materials_mode"] == "all" else
             int(len(cards) * config["materials_proportion"]) if config["materials_mode"] == "proportion" else 0)
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"agent", "model", "method", "cards"}:
            raise ValueError("Invalid assignment fields")
        if type(row["agent"]) is not int or not 1 <= row["agent"] <= config["agents"]:
            raise ValueError("Invalid assignment agent")
        agents.append(row["agent"])
        if row["model"] not in config["models"]:
            raise ValueError("Invalid assignment model")
        method = row["method"]
        if level == "baseline" or config["method_mode"] == "none":
            if method is not None:
                raise ValueError("Assignment method must be null")
        elif not isinstance(method, str) or method not in config["methods"]:
            raise ValueError("Invalid assignment method")
        text_list(row["cards"], "assignment cards")
        if len(row["cards"]) != count or len(set(row["cards"])) != count or not set(row["cards"]) <= cards:
            raise ValueError("Invalid assignment cards or material count")
    if (agents != sorted(set(agents)) or (returned_from is None and agents != list(range(1, config["agents"] + 1)))
            or (returned_from is not None and len(agents) != 1)):
        raise ValueError("Assignments require each agent once, or one collision-return agent")
    if config["method_mode"] == "same" and len({r["method"] for r in rows}) != 1:
        raise ValueError("Assignments violate same method mode")
    return deepcopy(rows)


def record_batch(exam, config, run_dir, *, seed, backend, ledger, rep=1, batch=1, level="round1",
                 forbidden=None, decompositions=None, previous=None, returned_from=None, returned_idea=None,
                 assignments=None, premises=None, purpose="generate"):
    positive_int(batch, "batch")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if level not in LEVELS:
        raise ValueError("Invalid generation level")
    # A bundled return lists one agent's collided originals; each child is traced to one of them by position.
    if returned_from is not None and not (isinstance(returned_from, str) and returned_from or
            isinstance(returned_from, list) and returned_from and len(set(returned_from)) == len(returned_from)
            and all(isinstance(key, str) and key for key in returned_from)):
        raise ValueError("returned_from must be an original idea id, a list of distinct ids, or null")
    if purpose not in ("generate", "rewrite") or (purpose == "rewrite" and returned_from is None):
        raise ValueError("purpose must be generate or a collision rewrite")
    if assignments is not None:
        assignments = checked_assignments(assignments, exam, config, level, returned_from)
    path = output_dir(run_dir, exam["id"], config["name"], rep)
    replacements.refuse_if_retired(run_dir, exam["id"], config["name"], rep)
    effective = {k: v for k, v in config.items() if k != "budget_usd"}
    identity = {"exam": digest(exam), "config": digest(effective), "seed": seed,
                "ledger": str(ledger.path), "backend": backend.name}
    with recording_lock(path):
        state = load_state(path, identity)
        state["guard_exam"] = {"planted": exam["planted"], "constraints": exam["constraints"]}
        if returned_from is not None and returned_idea is None:
            find = lambda key: next((idea for idea in state["ideas"] if idea["idea_id"] == key), None)
            returned_idea = [find(key) for key in returned_from] if isinstance(returned_from, list) else find(returned_from)
        sources = returned_from if isinstance(returned_from, list) else [returned_from]
        persist = lambda: save_state(path, state)
        batch_key = f"{batch}:{level}"
        allocation_seed = int(digest([seed, rep, batch, level]), 16)
        assignments = assignments if assignments is not None else allocate(exam, config, allocation_seed)
        if level == "baseline":
            assignments = [{**row, "method": None} for row in assignments]
        inputs = [forbidden, decompositions, previous, returned_from, returned_idea, assignments]
        if premises is not None or purpose != "generate":
            inputs.extend([premises, purpose])
        batch_identity = digest(inputs)
        current = state["batches"].setdefault(batch_key, {"identity": batch_identity, "assignments": assignments, "complete": False})
        if current["identity"] != batch_identity:
            raise ValueError("Batch identity conflict: generation context changed")
        persist()
        export_outputs(path, state, exam)
        for position, assignment in enumerate(assignments):
            peers = previous
            if config["serial_visible"]:
                peers = (previous or []) + [idea for idea in state["ideas"] if idea["batch"] == batch
                                            and idea["level"] == level and idea["agent"] < assignment["agent"]
                                            and idea["call_key"] in state["requests"]]
            prompts = current.setdefault("prompts", {})
            agent_key = str(assignment["agent"])
            if agent_key not in prompts:
                prompts[agent_key] = generation_prompt(exam, config, assignment, level=level, forbidden=forbidden,
                    decompositions=decompositions, previous=peers, returned_from=returned_from, returned_idea=returned_idea,
                    premises=premises)
                persist()
            prompt = prompts[agent_key]
            for attempt in (1, 2):
                key = digest([exam["id"], config["name"], rep, assignment["agent"], batch,
                              hashlib.sha256(prompt.encode()).hexdigest(), assignment["model"], attempt])
                request = state["requests"].get(key)
                if request and request["status"] in ("ok", "paid_error"):
                    break
                if request and request["status"] == "parse_error":
                    continue
                if request is None:
                    request = {"call_key": key, "exam_id": exam["id"], "config": config["name"], "rep": rep,
                               **{k: assignment[k] for k in ("agent", "model")}, "batch": batch, "level": level,
                               "purpose": purpose, "backend": backend.name, "attempt": attempt,
                               "cards": assignment["cards"],
                               "started_at": None, "duration_ms": None, "status": "pending", "receipt": None}
                    state["requests"][key] = request
                persist()
                measured = MeasuredBackend(backend, request, persist)
                print(f"Recording {exam['id']}/{config['name']} rep={rep} batch={batch} level={level} "
                       f"agent={assignment['agent']} assignment={position + 1}/{len(assignments)} attempt={attempt}", file=sys.stderr)
                try:
                    result = guarded_call(ledger, measured, key, prompt, assignment["model"], config["max_output_tokens"])
                except PaidCallError as exc:
                    sync_call(state, key, ledger)
                    request["status"] = "paid_error"
                    add_failure(state, "generate", key, "paid_error", str(exc))
                    persist()
                    export_outputs(path, state, exam)
                    break
                except (LedgerError, NotStarted) as exc:
                    sync_call(state, key, ledger)
                    add_failure(state, "generate", key, type(exc).__name__, str(exc))
                    persist()
                    export_outputs(path, state, exam)
                    if isinstance(exc, BudgetExceeded):
                        status = ledger.status()
                        reserve = amount(backend.estimate_max_usd(prompt, assignment["model"], config["max_output_tokens"]))
                        shortfall = max(Decimal(0), exact_sum(status["used"], reserve, status["limit"].copy_negate()))
                        raise BudgetExceeded(f"{exc}; shortfall_usd={shortfall}; pending_agents={len(assignments) - position}; "
                                             "pending_calls_at_least=1 (future actual costs unknown)") from exc
                    raise
                if measured.persistence_error is not None:
                    raise measured.persistence_error
                request["receipt"] = asdict(result)
                sync_call(state, key, ledger)
                try:
                    bundled = isinstance(returned_from, list)
                    rows = parse_ideas(result.text, assignment["cards"], len(sources) if bundled else None)
                    if purpose == "rewrite" and not bundled and len(rows) != 1:
                        raise ValueError("A collision rewrite must return exactly one idea")
                except ValueError as exc:
                    request["status"] = "parse_error"
                    add_failure(state, "parse", key, "invalid_output", str(exc))
                    persist()
                    export_outputs(path, state, exam)
                    continue
                if bundled and len(rows) < len(sources):
                    # Kept, not retried: the model may judge two originals one mechanism; the gap stays visible.
                    add_failure(state, "parse", key, "partial_return",
                                f"{len(sources) - len(rows)} of {len(sources)} originals got no replacement")
                for index, row in enumerate(rows):
                    row = dict(row)
                    source = sources[row.pop("replaces") - 1] if bundled else returned_from
                    state["ideas"].append({"idea_id": "i-" + hashlib.sha1((key + str(index)).encode()).hexdigest()[:12],
                                           "exam_id": exam["id"], "config": config["name"], "rep": rep,
                                           **assignment, "batch": batch, "level": level, "returned": returned_from is not None,
                                           "returned_from": source, **row, "call_key": key})
                request["status"] = "ok"
                persist()
                export_outputs(path, state, exam)
                break
        current["complete"] = True
        persist()
        return export_outputs(path, state, exam)


IDEA_FIELDS = {"idea_id", "exam_id", "config", "rep", "agent", "batch", "level", "model", "method",
               "cards", "cards_used", "returned", "returned_from", "title", "text", "decomposition", "call_key"}
CALL_FIELDS = {"call_key", "exam_id", "config", "rep", "agent", "level", "purpose", "backend", "model",
               "attempt", "started_at", "duration_ms", "tokens", "usd", "usd_estimated", "outcome"}


def validate_idea(idea):
    if not isinstance(idea, dict) or set(idea) != IDEA_FIELDS:
        raise ValueError("Idea: missing or unknown lineage fields")
    for field in ("exam_id", "config"):
        safe_segment(idea[field])
    for field in ("idea_id", "call_key", "model", "title", "text"):
        if not isinstance(idea[field], str) or not idea[field].strip():
            raise ValueError(f"Idea: invalid {field}")
    if not re.fullmatch(r"i-[0-9a-f]{12}", idea["idea_id"]):
        raise ValueError("Idea: invalid idea_id format")
    for field in ("rep", "agent", "batch"):
        positive_int(idea[field], field)
    if idea["level"] not in LEVELS or (idea["method"] is not None and not isinstance(idea["method"], str)):
        raise ValueError("Idea: invalid level or method")
    for field in ("cards", "cards_used"):
        text_list(idea[field], field)
        if len(set(idea[field])) != len(idea[field]):
            raise ValueError(f"Idea: duplicate {field}")
    if not set(idea["cards_used"]) <= set(idea["cards"]):
        raise ValueError("Idea: unassigned cards_used")
    if (type(idea["returned"]) is not bool or
            (idea["returned"] and (not isinstance(idea["returned_from"], str) or not idea["returned_from"])) or
            (not idea["returned"] and idea["returned_from"] is not None)):
        raise ValueError("Idea: inconsistent return lineage")
    if idea["decomposition"] is not None:
        text_list(idea["decomposition"], "decomposition")


def validate_call(call):
    if not isinstance(call, dict) or set(call) != CALL_FIELDS:
        raise ValueError("Call receipt: missing or unknown fields, including explicit usd")
    for field in ("exam_id", "config"):
        safe_segment(call[field])
    for field in ("call_key", "model", "backend"):
        if not isinstance(call[field], str) or not call[field].strip():
            raise ValueError(f"Call receipt: invalid {field}")
    if call["started_at"] is not None:
        try:
            datetime.fromisoformat(call["started_at"])
        except (ValueError, TypeError) as exc:
            raise ValueError("Call receipt: invalid started_at") from exc
    for field in ("rep", "agent", "attempt"):
        positive_int(call[field], field)
    if call["duration_ms"] is not None and (type(call["duration_ms"]) is not int or call["duration_ms"] < 0):
        raise ValueError("Call receipt: invalid duration_ms")
    if (call["level"] not in LEVELS or call["purpose"] not in ("generate", "dedupe", "rewrite", "judge", "constraint")
            or call["outcome"] not in ("ok", "paid_error") or type(call["usd_estimated"]) is not bool):
        raise ValueError("Call receipt: invalid level, purpose, outcome or usd_estimated")
    if amount(call["usd"]) == 0 and call["usd_estimated"]:
        raise ValueError("Call receipt: a conservative estimated reservation cannot be zero")
    if call["outcome"] == "paid_error" and call["usd_estimated"]:
        raise ValueError("Call receipt: failed response with unknown actual billing requires reconciliation")
    token_counts(call["tokens"])
    if set(call["tokens"]) != {"input", "cache_write", "cache_read", "output"} and not (call["outcome"] == "paid_error" and call["tokens"] == {}):
        raise ValueError("Call receipt: missing token fields")


class ReceiptBackend:
    """Import an already incurred external charge; never invoke a model."""

    name = "receipt"

    def __init__(self, receipt):
        self.receipt = receipt

    def estimate_max_usd(self, *_):
        # The ledger requires a positive reserve even for explicit zero-charge receipts.
        return amount(self.receipt["usd"]) or Decimal("0.000000001")

    def call(self, *_):
        return CallResult(text=dumps(self.receipt),
                          usd=None if self.receipt["usd_estimated"] else amount(self.receipt["usd"]),
                          tokens=self.receipt["tokens"], outcome=self.receipt["outcome"])


def receipt_matches(call, event):
    return (event["event"] == "settle" and amount(event["usd"]) == amount(call["usd"])
            and event["tokens"] == call["tokens"] and event["estimated"] == call["usd_estimated"]
            and event.get("outcome") == call["outcome"])


def import_results(ideas_path, calls_path, run_dir, *, ledger):
    ideas, calls = read_rows(ideas_path), read_rows(calls_path)
    if not Path(ideas_path).is_file() or not calls:
        raise ValueError("Import requires an Idea JSONL file and nonempty Call receipts")
    call_map = {}
    for call in calls:
        validate_call(call)
        key = call["call_key"]
        if key in call_map:
            raise ValueError(f"Duplicate call receipt: {key}")
        call_map[key] = call
    ordinals = {}
    for idea in ideas:
        validate_idea(idea)
        key = idea["call_key"]
        ordinal = ordinals.get(key, 0)
        expected_id = "i-" + hashlib.sha1((key + str(ordinal)).encode()).hexdigest()[:12]
        if idea["idea_id"] != expected_id:
            raise ValueError(f"Idea: idea_id does not match call_key and output ordinal {ordinal}")
        ordinals[key] = ordinal + 1
        call = call_map.get(idea["call_key"])
        if call is None or call["outcome"] != "ok":
            raise ValueError(f"Missing successful call receipt for {idea['call_key']}")
        for field in ("exam_id", "config", "rep", "agent", "level", "model"):
            if call[field] != idea[field]:
                raise ValueError(f"Idea/Call lineage conflict: {field}")
    units = {}
    for call in calls:
        path = output_dir(run_dir, call["exam_id"], call["config"], call["rep"])
        units.setdefault(path, []).append(call)
    # Lock every affected unit and validate all persisted conflicts before any charge or commit.
    with ExitStack() as stack:
        states = {}
        last_events = {event["call_key"]: event for event in read_rows(ledger.path)}
        for call in calls:
            if call["call_key"] in last_events:
                raise ValueError(f"Call {call['call_key']} is already a recorded call; "
                                 "import cannot reuse or relabel recording lineage")
            billed_key = "import_" + digest(call["call_key"])
            event = last_events.get(billed_key)
            if event is not None and event["event"] != "release" and not receipt_matches(call, event):
                raise ValueError(f"Conflicting or unresolved import ledger receipt {billed_key}; "
                                 "for an unfinished reservation, confirm bookkeeping was not charged, then use "
                                 f"ledger release {billed_key} --confirm-not-charged on this ledger before retrying. "
                                 "An already settled conflicting receipt needs a policy decision; do not reset the ledger")
        for path in sorted(units):
            stack.enter_context(recording_lock(path))
            identity = {"import": True, "ledger": str(ledger.path)}
            state = load_state(path, None if (path / "record_state.json").exists() else identity)
            if not state["identity"].get("import"):
                raise ValueError("Target is a recording unit; external imports must use a separate import-only unit. "
                                 "Merging or relabelling recorded calls is forbidden")
            if state["identity"]["ledger"] != str(ledger.path):
                raise ValueError("Import ledger identity conflict")
            states[path] = state
            existing_ideas = {i["idea_id"]: i for i in state["ideas"]}
            existing_calls = {c["call_key"]: c for c in state["calls"]}
            for call in units[path]:
                if call["call_key"] in existing_calls and existing_calls[call["call_key"]] != call:
                    raise ValueError("Conflicting persisted Call receipt")
                if call["call_key"] in existing_calls and (
                        [idea for idea in state["ideas"] if idea["call_key"] == call["call_key"]]
                        != [idea for idea in ideas if idea["call_key"] == call["call_key"]]):
                    raise ValueError("Committed external calls are immutable; reimport requires identical ideas")
                old = state.get("import_receipts", {}).get(call["call_key"])
                if old is not None and old != call:
                    raise ValueError("Conflicting pending import receipt")
                cached_path = ledger.cache_path("import_" + digest(call["call_key"]))
                if cached_path.exists() and read_file(cached_path)["result"]["text"] != dumps(call):
                    raise ValueError("Conflicting external receipt already present in the ledger")
            for idea in ideas:
                if idea["idea_id"] in existing_ideas and existing_ideas[idea["idea_id"]] != idea:
                    raise ValueError("Conflicting persisted Idea")
        for path, unit_calls in units.items():
            state = states[path]
            for call in unit_calls:
                key = call["call_key"]
                state.setdefault("import_receipts", {})[key] = call
                save_state(path, state)
                adapter = ReceiptBackend(call)
                try:
                    result = guarded_call(ledger, adapter, "import_" + digest(key), dumps(call), call["model"], 1)
                except PaidCallError:
                    if call["outcome"] != "paid_error":
                        raise
                    event = next(row for row in reversed(read_rows(ledger.path)) if row["call_key"] == "import_" + digest(key))
                    if not receipt_matches(call, event):
                        raise ValueError("Conflicting paid-error ledger receipt")
                    result = None
                except BudgetExceeded as exc:
                    raise BudgetExceeded(f"External costs already incurred; receipt {key} not imported; {exc}") from exc
                if result is not None and result.text != dumps(call):
                    raise ValueError(f"External call_key {key} has a conflicting ledger receipt")
                if not any(c["call_key"] == key for c in state["calls"]):
                    state["calls"].append(call)
                existing = {i["idea_id"] for i in state["ideas"]}
                state["ideas"].extend(i for i in ideas if i["call_key"] == key and i["idea_id"] not in existing)
                state["requests"]["import_" + key] = {"call_key": key, "external": True,
                    "receipt": {"text": dumps([i for i in ideas if i["call_key"] == key])} if call["outcome"] == "ok" else None}
                save_state(path, state)
                export_outputs(path, state, {"planted": [], "constraints": ["External guards not evaluated"]})
    return {"ideas": len(ideas), "calls": len(calls)}


def fake_generation(prompt, model, max_output_tokens):
    tag = hashlib.sha256(f"{prompt}:{model}:{max_output_tokens}".encode()).hexdigest()[:12]
    return dumps({"ideas": [{"title": f"Test idea {index}", "text": f"Test only mechanism {tag}/{index}",
                              "cards_used": [], "decomposition": None} for index in range(10)]})


def make_backend(config):
    options = dict(config["backend"])
    kind = options.pop("kind")
    if kind == "fake":
        options.setdefault("responses", fake_generation)
        return FakeBackend(**options)
    if kind == "command":
        return CommandBackend(**options)
    return ClaudeCliBackend(**options)


def run_record(args):
    exams = load_exams(args.exam_dir)
    config = load_config(args.config)
    if args.run_dir is None:
        args.run_dir = Path(__file__).resolve().parent / "runs" / config["name"]
    root = Path(args.exam_dir).resolve()
    if Path(args.run_dir).resolve().is_relative_to(root):
        raise ValueError("run-dir must not be inside the read-only exam-dir")
    backend = make_backend(config)
    ledger = BudgetLedger(args.ledger or default_ledger_path(backend.name, args.run_dir), args.limit or config["budget_usd"])
    selected = [exam for exam in exams if args.exam_id is None or exam["id"] == args.exam_id]
    if not selected:
        raise ValueError("exam-id was not found")
    for exam in selected:
        try:
            summary = record_batch(exam, config, args.run_dir, seed=args.seed, backend=backend, ledger=ledger,
                                   rep=args.rep, batch=args.batch, level=args.level)
        except NotStarted as exc:
            raise ValueError(f"Recording stopped before provider startup: {exc}") from exc
        print(dumps(summary))


def register(subparsers):
    parser = subparsers.add_parser("record", help="Record a single generation batch through the ledger")
    parser.add_argument("--exam-dir", type=Path, required=True)
    parser.add_argument("--exam-id")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, help="Default: evals/brainstorm/runs/<config name>/")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--level", choices=LEVELS, default="round1")
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--limit")
    parser.set_defaults(func=lambda args: cli_boundary(run_record, args))
    parser = subparsers.add_parser("import_results", help="Import Idea JSONL and external cost receipts")
    parser.add_argument("--ideas", type=Path, required=True)
    parser.add_argument("--calls", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--limit", default="50")
    parser.set_defaults(func=lambda args: cli_boundary(lambda args: print(dumps(import_results(
        args.ideas, args.calls, args.run_dir, ledger=BudgetLedger(args.ledger, args.limit)))), args))
