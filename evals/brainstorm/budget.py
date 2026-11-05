"""Persistent preflight reservations and conservative crash recovery (POSIX)."""

from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from .backends import CallResult, NotStarted, amount, exact_sum, token_counts, validate_result


class LedgerError(RuntimeError):
    pass


class BudgetExceeded(LedgerError):
    exit_code = 3


class CallBusy(LedgerError):
    pass


class CallUncertain(LedgerError):
    pass


class PaidCallError(LedgerError):
    pass


_PROCESS_FAILURE = None


def _ensure_active():
    if _PROCESS_FAILURE is not None:
        message, overrun = _PROCESS_FAILURE
        raise (BudgetExceeded if overrun else LedgerError)(message)


def _stop_process(key, reserved, usd, reason):
    global _PROCESS_FAILURE
    delta = exact_sum(usd, reserved.copy_negate()) if usd > reserved else Decimal(0)
    _PROCESS_FAILURE = (f"Call {key} billing could not persist: reserved={reserved} actual={usd} "
                        f"delta={delta}; process halted; {reason}", usd > reserved)


def dumps(value):
    """Write Decimal as exact JSON numbers, not binary floats or quoted strings."""
    if isinstance(value, Decimal):
        return str(amount(value))
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":" + dumps(v) for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(dumps(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending_", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def real_ledger_path():
    """Outside every worktree, so cleaning or switching one can neither lose nor split the real ledger."""
    configured = os.environ.get("XDG_STATE_HOME", "")
    # The XDG spec says a relative value is invalid and must be ignored.
    state = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local" / "state"
    return state / "xiaolang-skills" / "brainstorm" / "ledger.real.jsonl"


def legacy_ledger_path(*, repo_root=None):
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    return root / "evals" / "brainstorm" / "runs" / "ledger.real.jsonl"


def default_ledger_path(backend, run_dir, *, repo_root=None):
    if backend == "fake":
        return Path(run_dir) / "ledger.fake.jsonl"
    path, old = real_ledger_path(), legacy_ledger_path(repo_root=repo_root)
    if old.exists() and not path.exists():
        # An empty ledger here would reset the spending limit, so nothing is created and nothing is moved.
        raise LedgerError(f"The real ledger moved. Found the old one at {old} and none at {path}. Stop all runs, then move "
                          f"it with its .lock and .cache: mkdir -p '{path.parent}' && mv '{old}' '{old}.lock' '{old}.cache' "
                          f"'{path.parent}/'. A run that must stay on the old file can pass --ledger.")
    return path


class BudgetLedger:
    def __init__(self, path, limit="50"):
        self.path = Path(path).resolve()
        self.limit = amount(limit)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.cache_dir = self.path.with_name(self.path.name + ".cache")

    def cache_path(self, call_key):
        return self.cache_dir / (hashlib.sha256(call_key.encode("utf-8")).hexdigest() + ".json")

    @contextmanager
    def _locked(self):
        _ensure_active()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Unbuffered writes cannot re-raise a failed evidence flush during close.
        with self.lock_path.open("a+b", buffering=0) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                _ensure_active()
                lock.seek(0)
                if lock.read():
                    raise LedgerError("Ledger halt: billing persistence failed; reconcile lock evidence before reuse")
                yield lock
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        states = {}
        if not self.path.exists():
            if self.cache_dir.exists() and any(self.cache_dir.iterdir()):
                raise LedgerError("Missing ledger with surviving cache; refusing to reset budget")
            return states
        line_number = 1
        # ceiling: whole-ledger scan for small evaluation batches; index when this dominates runtime.
        try:
            text = self.path.read_text(encoding="utf-8")
            if not text:
                raise ValueError("Empty ledger")
            if not text.endswith("\n"):
                raise ValueError("Truncated final line")
            for line_number, line in enumerate(text.splitlines(), 1):
                event = json.loads(line, parse_float=Decimal)
                kind, key = event["event"], event["call_key"]
                if not isinstance(key, str) or not key or kind not in {"reserve", "settle", "release", "uncertain"}:
                    raise ValueError("Invalid event or key")
                event["usd"] = amount(event["usd"])
                token_counts(event["tokens"])
                if type(event["estimated"]) is not bool or type(event["pid"]) is not int or event["pid"] <= 0:
                    raise ValueError("Invalid event metadata")
                if not isinstance(event["ts"], str) or not event["ts"]:
                    raise ValueError("Missing timestamp")
                previous = states.get(key)
                previous_kind = previous["last"]["event"] if previous else None
                if kind == "reserve":
                    if previous_kind not in (None, "release"):
                        raise ValueError("Reservation already exists")
                    states[key] = {"reserve": event, "last": event}
                else:
                    if previous_kind not in {"reserve", "uncertain"} or (kind == "uncertain" and previous_kind != "reserve"):
                        raise ValueError("Invalid event sequence")
                    if kind in {"release", "uncertain"} and event["usd"] != previous["reserve"]["usd"]:
                        raise ValueError("Reservation amount changed")
                    if kind == "settle" and event.get("outcome") not in {"ok", "paid_error"}:
                        raise ValueError("Invalid settlement outcome")
                    previous["last"] = event
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise LedgerError(f"Invalid ledger {self.path.name} near line {line_number}: {exc}") from exc
        return states

    def _append(self, kind, key, usd, *, tokens=None, estimated=False, outcome=None):
        event = {"event": kind, "call_key": key, "usd": amount(usd), "tokens": tokens or {},
                 "estimated": estimated, "pid": os.getpid(), "ts": datetime.now(timezone.utc).isoformat()}
        if outcome is not None:
            event["outcome"] = outcome
        prefix = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        atomic_write(self.path, prefix + dumps(event) + "\n")

    def _cache(self, key, state):
        path = self.cache_path(key)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
            if value["call_key"] != key or state is None or value["reserved"] != state["reserve"]["usd"]:
                raise ValueError("Cache does not match reservation")
            result = CallResult(**value["result"])
            validate_result(result)
            if result.outcome != "ok":
                raise ValueError("Invalid cached result")
            usd = amount(value["usd"])
            estimated = value["estimated"]
            if type(estimated) is not bool or estimated != (result.usd is None):
                raise ValueError("Invalid estimated flag")
            if usd != (state["reserve"]["usd"] if estimated else amount(result.usd)):
                raise ValueError("Cache billing mismatch")
            last = state["last"]
            if last["event"] == "settle" and (last["usd"] != usd or last["estimated"] != estimated
                                                  or last["tokens"] != result.tokens or last["outcome"] != "ok"):
                raise ValueError("Cache does not match settlement")
            return value, result
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise LedgerError(f"Invalid cache for {key}: {exc}") from exc

    def _recover(self, states):
        changed = False
        for key, state in states.items():
            last = state["last"]
            if last["event"] != "reserve" or alive(state["reserve"]["pid"]):
                continue
            cached = self._cache(key, state)
            if cached:
                value, result = cached
                self._append("settle", key, value["usd"], tokens=result.tokens,
                             estimated=value["estimated"], outcome="ok")
            else:
                self._append("uncertain", key, state["reserve"]["usd"])
            changed = True
        return self._read() if changed else states

    def _status(self, states):
        settled = reserved = uncertain = estimated = Decimal(0)
        overruns = []
        for key, state in states.items():
            event, reserve = state["last"], state["reserve"]["usd"]
            usd, kind = event["usd"], event["event"]
            if kind == "settle":
                settled = exact_sum(settled, usd)
                estimated = exact_sum(estimated, usd if event["estimated"] else 0)
                if usd > reserve:
                    overruns.append({"call_key": key, "reserved": reserve, "actual": usd, "delta": exact_sum(usd, reserve.copy_negate())})
            elif kind == "reserve":
                reserved = exact_sum(reserved, usd)
            elif kind == "uncertain":
                uncertain = exact_sum(uncertain, usd)
        used = exact_sum(settled, reserved, uncertain)
        return {"limit": self.limit, "used": used, "settled": settled, "reserved": reserved,
                "uncertain": uncertain, "estimated": estimated,
                "remaining": max(Decimal(0), exact_sum(self.limit, used.copy_negate())), "overruns": overruns}

    def status(self):
        with self._locked():
            return self._status(self._recover(self._read()))

    def reserve(self, key, usd):
        usd = amount(usd)
        if not isinstance(key, str) or not key:
            raise ValueError("call_key must be a nonempty string")
        with self._locked():
            states = self._recover(self._read())
            state = states.get(key)
            cached = self._cache(key, state)
            if state:
                kind = state["last"]["event"]
                if kind == "reserve":
                    raise CallBusy(f"Call {key} is owned by live pid {state['reserve']['pid']}")
                if kind == "uncertain":
                    raise CallUncertain(f"Call {key} has unknown billing; use ledger settle or release with confirmation")
                if kind == "settle":
                    if state["last"]["outcome"] == "paid_error":
                        raise PaidCallError(f"Call {key} already paid and failed; retry with a new attempt key")
                    if not cached:
                        raise LedgerError(f"Missing successful cache for {key}; refusing to resend")
                    return cached[1]
                if cached:
                    raise LedgerError(f"Released call {key} unexpectedly has a cache")
            status = self._status(states)
            if status["overruns"] or exact_sum(status["used"], usd) > self.limit:
                raise BudgetExceeded(f"Budget stopped: used={status['used']} reserve={usd} limit={self.limit} overruns={dumps(status['overruns'])}")
            self._append("reserve", key, usd)
        return None

    def _write_cache(self, key, reserved, result):
        validate_result(result)
        atomic_write(self.cache_path(key), dumps({"call_key": key, "reserved": reserved,
                     "usd": reserved if result.usd is None else amount(result.usd),
                     "estimated": result.usd is None, "result": asdict(result)}) + "\n")

    def settle(self, key, usd, *, tokens=None, estimated=False, outcome="ok", manual=True):
        usd = amount(usd)
        tokens = token_counts({} if tokens is None else tokens)
        if type(estimated) is not bool or outcome not in {"ok", "paid_error"}:
            raise ValueError("Invalid settlement metadata")
        with self._locked() as lock:
            states = self._recover(self._read()) if manual else self._read()
            state = states.get(key)
            if not state:
                raise LedgerError(f"No reservation for {key}")
            last = state["last"]
            if last["event"] == "settle":
                if last["usd"] == usd:
                    return
                raise LedgerError(f"Call {key} already settled with a different amount")
            if last["event"] == "release":
                raise LedgerError(f"Call {key} was released")
            owner = state["reserve"]["pid"]
            if last["event"] == "reserve" and alive(owner) and (manual or owner != os.getpid()):
                raise CallBusy(f"Cannot settle live call {key}")
            if manual and usd == 0:
                raise LedgerError("Use release --confirm-not-charged for a zero-charge reconciliation")
            reserved = state["reserve"]["usd"]
            try:
                if not manual and outcome == "paid_error":
                    # Only the owner may discard its failed output before recording the charge.
                    self.cache_path(key).unlink(missing_ok=True)
                    cached = None
                else:
                    cached = self._cache(key, state)
                if cached:
                    value, result = cached
                    if usd != value["usd"]:
                        raise LedgerError(f"Settlement disagrees with cached cost for {key}")
                    tokens, estimated, outcome = result.tokens, value["estimated"], "ok"
                elif manual:
                    # Reconciled billing is not a successful generation result.
                    outcome = "paid_error"
                self._append("settle", key, usd, tokens=tokens, estimated=estimated, outcome=outcome)
            except OSError as exc:
                _stop_process(key, reserved, usd, f"settlement failed ({type(exc).__name__}, errno={exc.errno})")
                try:
                    self._halt_locked(lock, key, reserved, usd)
                except Exception as halt_error:
                    _stop_process(key, reserved, usd, f"settlement and halt writes failed ({type(halt_error).__name__}, errno={getattr(halt_error, 'errno', None)})")
                _ensure_active()
            if usd > reserved:
                delta = exact_sum(usd, reserved.copy_negate())
                raise BudgetExceeded(f"Reservation exceeded for {key}: reserved={reserved} actual={usd} delta={delta}")

    def mark_uncertain(self, key):
        with self._locked():
            states = self._read()
            state = states.get(key)
            if not state or state["last"]["event"] != "reserve":
                raise LedgerError(f"No pending reservation for {key}")
            if state["reserve"]["pid"] != os.getpid():
                raise CallBusy(f"Cannot change another owner's call {key}")
            self._append("uncertain", key, state["reserve"]["usd"])

    def _halt_locked(self, lock, key, reserved, usd):
        # Keep the settlement lock until existing-file evidence is durable or has failed.
        data = (dumps({"call_key": key, "reserved": reserved, "usd": usd,
                       "reason": "billing persistence failed"}) + "\n").encode("utf-8")
        if lock.write(data) != len(data):
            raise OSError("Incomplete halt evidence")
        os.fsync(lock.fileno())

    def release(self, key, *, confirm_not_charged=False, manual=True):
        if not confirm_not_charged:
            raise LedgerError("release requires --confirm-not-charged")
        with self._locked():
            states = self._recover(self._read())
            state = states.get(key)
            if not state:
                raise LedgerError(f"No reservation for {key}")
            kind = state["last"]["event"]
            if kind == "release":
                return
            if kind == "settle":
                raise LedgerError(f"Cannot release settled call {key}")
            owner = state["reserve"]["pid"]
            if kind == "reserve" and alive(owner) and (manual or owner != os.getpid()):
                raise CallBusy(f"Cannot release live call {key}")
            if self._cache(key, state):
                raise LedgerError(f"Cannot release call {key} with a successful cache")
            self._append("release", key, state["reserve"]["usd"])


def guarded_call(ledger, backend, call_key, prompt, model, max_output_tokens):
    _ensure_active()
    reserved = amount(backend.estimate_max_usd(prompt, model, max_output_tokens))
    if reserved <= 0:
        raise ValueError("Reservation must be positive; configure a conservative model price")
    cached = ledger.reserve(call_key, reserved)
    if cached is not None:
        return cached
    _ensure_active()
    try:
        result = backend.call(prompt, model, max_output_tokens)
    except NotStarted:
        ledger.release(call_key, confirm_not_charged=True, manual=False)
        raise
    except Exception as exc:
        ledger.mark_uncertain(call_key)
        raise CallUncertain(f"Call {call_key} may be charged; use ledger settle or release with confirmation") from exc
    try:
        usd = reserved if result.usd is None else amount(result.usd)
    except (ValueError, AttributeError) as exc:
        ledger.mark_uncertain(call_key)
        raise CallUncertain(f"Call {call_key} returned invalid billing; reconcile manually") from exc
    try:
        validate_result(result)
        valid_result = True
    except (ValueError, TypeError):
        valid_result = False
    if result.usd is None and (not valid_result or result.outcome != "ok"):
        ledger.mark_uncertain(call_key)
        raise CallUncertain(f"Call {call_key} has no usable result or cost; reconcile manually")
    if not valid_result:
        result = CallResult(text="", usd=usd, outcome="paid_error")
    if result.outcome == "ok":
        try:
            ledger._write_cache(call_key, reserved, result)
        except (OSError, ValueError, TypeError) as exc:
            if result.usd is None:
                ledger.mark_uncertain(call_key)
                raise CallUncertain(f"Call {call_key} cache failed and cost is unknown; reconcile manually") from exc
            result = replace(result, outcome="paid_error")
    try:
        ledger.settle(call_key, usd, tokens=result.tokens, estimated=result.usd is None,
                      outcome=result.outcome, manual=False)
    except BudgetExceeded:
        raise
    except Exception as exc:
        if _PROCESS_FAILURE is None:
            _stop_process(call_key, reserved, usd, f"settlement failed ({type(exc).__name__})")
        _ensure_active()
    if result.outcome == "paid_error":
        raise PaidCallError(f"Call {call_key} failed and was charged {usd}; use a new attempt key")
    return result


def cli_boundary(function, args):
    try:
        function(args)
        return 0
    except BudgetExceeded as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except (LedgerError, ValueError, OSError, DecimalException) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _run(args):
    ledger = BudgetLedger(args.ledger or default_ledger_path("claude", "."), args.limit)
    if args.action == "settle":
        ledger.settle(args.call_key, args.usd)
    elif args.action == "release":
        ledger.release(args.call_key, confirm_not_charged=args.confirm_not_charged)
    print(dumps(ledger.status()))


def register(subparsers):
    parser = subparsers.add_parser("ledger", help="Inspect or reconcile model billing")
    parser.add_argument("--ledger", type=Path, help="Default: the real ledger")
    parser.add_argument("--limit", default="50")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("status")
    settle = actions.add_parser("settle")
    settle.add_argument("call_key")
    settle.add_argument("--usd", required=True)
    release = actions.add_parser("release")
    release.add_argument("call_key")
    release.add_argument("--confirm-not-charged", action="store_true")
    parser.set_defaults(func=lambda args: cli_boundary(_run, args))
