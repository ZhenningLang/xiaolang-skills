"""Model boundary: no tools, no automatic retries, explicit billing failures."""

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, getcontext, localcontext
from typing import Protocol


def amount(value):
    """Parse finite nonnegative USD without binary floating point arithmetic."""
    if isinstance(value, bool):
        raise ValueError(f"Invalid USD amount: {value!r}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid USD amount: {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"Invalid USD amount: {value!r}")
    if number and not getcontext().Emin <= number.adjusted() <= getcontext().Emax:
        raise ValueError("USD amount is outside the supported decimal exponent range")
    return number


def token_counts(value):
    if not isinstance(value, dict) or any(
        key not in {"input", "cache_write", "cache_read", "output"}
        or isinstance(n, bool) or not isinstance(n, int) or n < 0 for key, n in value.items()
    ):
        raise ValueError("Invalid token counts")
    return value


def exact_sum(*values):
    """Avoid silently rounding long configured amounts at Decimal's default precision."""
    values = [Decimal(value) for value in values]
    with localcontext() as context:
        context.prec = max(28, max(value.adjusted() for value in values)
                           - min(value.as_tuple().exponent for value in values)
                           + len(str(len(values))) + 2)
        return sum(values, Decimal(0))


class NotStarted(RuntimeError):
    """The request was definitely not sent; its reservation can be released."""


class ResultUnknown(RuntimeError):
    """The request may have been charged; keep its reservation."""


@dataclass(frozen=True)
class CallResult:
    text: str
    usd: Decimal | None
    tokens: dict = field(default_factory=dict)
    raw_ref: str | None = None
    outcome: str = "ok"


def validate_result(result):
    if not isinstance(result, CallResult) or not isinstance(result.text, str):
        raise ValueError("Invalid backend result text")
    if result.outcome not in {"ok", "paid_error"}:
        raise ValueError("Invalid backend outcome")
    if result.raw_ref is not None and not isinstance(result.raw_ref, str):
        raise ValueError("Invalid raw reference")
    token_counts(result.tokens)
    if result.usd is not None:
        amount(result.usd)


class Backend(Protocol):
    name: str

    def estimate_max_usd(self, prompt: str, model: str, max_output_tokens: int) -> Decimal: ...

    def call(self, prompt: str, model: str, max_output_tokens: int) -> CallResult: ...


class FakeBackend:
    name = "fake"

    def __init__(self, *, usd="0.01", reserve_usd="0.02", failure=None):
        self.usd = None if usd is None else Decimal(str(usd))
        self.reserve_usd = Decimal(str(reserve_usd))
        self.failure = failure
        self.calls = 0

    def estimate_max_usd(self, prompt, model, max_output_tokens):
        return self.reserve_usd

    def call(self, prompt, model, max_output_tokens):
        if self.failure == "not_started":
            raise NotStarted("Fake request did not start")
        self.calls += 1
        if self.failure == "unknown":
            raise ResultUnknown("Fake response unreadable")
        return CallResult(
            text=hashlib.sha256(f"{prompt}:{model}:{max_output_tokens}".encode()).hexdigest(),
            usd=None if self.failure == "missing_cost" else self.usd,
            tokens={"input": len(prompt.encode()), "cache_write": 0, "cache_read": 0, "output": 1},
            outcome="paid_error" if self.failure == "paid_error" else "ok",
        )


class ClaudeCliBackend:
    name = "claude"

    def __init__(self, *, pricing, executable="claude", timeout=120):
        self.pricing = pricing
        self.executable = executable
        self.timeout = timeout
        self.last_response = None

    def estimate_max_usd(self, prompt, model, max_output_tokens):
        if model not in self.pricing:
            raise ValueError(f"No configured price for model {model!r}")
        price = self.pricing[model]
        # UTF-8 bytes conservatively estimate visible input; hidden context is configured.
        input_tokens = exact_sum(amount(price["fixed_tokens"]), len(prompt.encode("utf-8")))
        quantities = [input_tokens, amount(price["input_per_million"]),
                      amount(max_output_tokens), amount(price["output_per_million"])]
        with localcontext() as context:
            context.prec = max(28, sum(len(value.as_tuple().digits) + abs(value.as_tuple().exponent)
                                      for value in quantities) + 10)
            return (quantities[0] * quantities[1] + quantities[2] * quantities[3]) / Decimal(1_000_000)

    def call(self, prompt, model, max_output_tokens):
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise NotStarted("max_output_tokens must be a positive integer")
        argv = [self.executable, "-p", "--setting-sources", "", "--strict-mcp-config",
                "--disable-slash-commands", "--tools", "", "--model", model,
                "--output-format", "json", "--no-session-persistence"]
        environment = os.environ.copy()
        environment["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output_tokens)
        self.last_response = None
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding="utf-8", env=environment)
        except OSError as exc:
            raise NotStarted(f"Could not start model process: {exc}") from exc
        with process:
            try:
                stdout, _ = process.communicate(input=prompt, timeout=self.timeout)
            except (subprocess.TimeoutExpired, OSError, UnicodeError) as exc:
                if process.poll() is None:
                    process.kill()
                raise ResultUnknown("Model process started but response is unreadable") from exc
        self.last_response = stdout
        try:
            payload = json.loads(stdout, parse_float=Decimal)
            if not isinstance(payload, dict):
                raise ValueError("Response is not an object")
            usd = payload.get("total_cost_usd")
            usd = None if usd is None else amount(usd)
        except (ValueError, TypeError) as exc:
            raise ResultUnknown("Model response has unknown billing") from exc
        paid_error = bool(payload.get("is_error")) or process.returncode != 0
        text = payload.get("result")
        if not isinstance(text, str):
            text, paid_error = "", True
        try:
            usage = payload["usage"]
            tokens = token_counts({key: usage[source] for key, source in (
                ("input", "input_tokens"), ("cache_write", "cache_creation_input_tokens"),
                ("cache_read", "cache_read_input_tokens"), ("output", "output_tokens"))})
        except (ValueError, TypeError, KeyError):
            tokens, paid_error = {}, True
        if paid_error and usd is None:
            raise ResultUnknown("Failed model response has unknown billing")
        return CallResult(text=text, usd=usd, tokens=tokens,
                          outcome="paid_error" if paid_error else "ok")
