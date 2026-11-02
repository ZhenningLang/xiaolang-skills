"""Explicit experiment switches; no model or provider auto-discovery."""

from copy import deepcopy

from .backends import amount
from .exam import read_json, safe_segment, text_list


LEVELS = ("baseline", "round1", "round2", "pressure1", "pressure2", "pressure3", "pressure4")
DEFAULTS = {"agents": 3, "method_mode": "none", "methods": {}, "materials_mode": "all",
            "materials_proportion": 1, "three_step": False, "predict_peers": False,
            "serial_visible": False, "return_collisions": False, "before_generation": False,
            "decomposition_timing": "none", "levels": ["round1"], "stop_on_exhaustion": False,
            "max_batches": 1, "max_output_tokens": 2048, "budget_usd": "50",
            "backend": {"kind": "fake"}}


def positive_int(value, field):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field}: expected a positive integer")


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError("config: expected an object")
    unknown = value.keys() - (DEFAULTS.keys() | {"name", "default_model", "models"})
    if unknown:
        raise ValueError(f"Unknown config fields: {sorted(unknown)}")
    result = {**deepcopy(DEFAULTS), **value}
    safe_segment(result.get("name"))
    if not isinstance(result.get("default_model"), str) or not result["default_model"].strip():
        raise ValueError("default_model: required nonempty model name")
    result["models"] = value.get("models", [result["default_model"]])
    text_list(result["models"], "models")
    if not result["models"] or len(set(result["models"])) != len(result["models"]) or result["default_model"] not in result["models"]:
        raise ValueError("models: unique nonempty list must include default_model")
    for key in ("agents", "max_batches", "max_output_tokens"):
        positive_int(result[key], key)
    for key in ("three_step", "predict_peers", "serial_visible", "return_collisions",
                "before_generation", "stop_on_exhaustion"):
        if type(result[key]) is not bool:
            raise ValueError(f"{key}: expected a strict boolean")
    for key, choices in (("method_mode", ("none", "same", "different")),
                         ("materials_mode", ("none", "all", "proportion")),
                         ("decomposition_timing", ("early", "none", "late"))):
        if result[key] not in choices:
            raise ValueError(f"{key}: expected one of {choices}")
    proportion = result["materials_proportion"]
    if type(proportion) not in (int, float) or not 0 <= proportion <= 1:
        raise ValueError("materials_proportion: expected a number in [0, 1]")
    methods = result["methods"]
    if not isinstance(methods, dict) or any(not isinstance(k, str) or not k.strip() or
                                           not isinstance(v, str) or not v.strip() for k, v in methods.items()):
        raise ValueError("methods: expected names mapped to nonempty instructions")
    if result["method_mode"] != "none" and not methods:
        raise ValueError("methods: selected mode requires method text")
    if (not isinstance(result["levels"], list) or not result["levels"] or
            any(x not in LEVELS for x in result["levels"]) or len(set(result["levels"])) != len(result["levels"])):
        raise ValueError("levels: expected a unique nonempty subset of supported levels")
    if amount(result["budget_usd"]) <= 0:
        raise ValueError("budget_usd: expected a positive amount")
    backend = result["backend"]
    if not isinstance(backend, dict) or backend.get("kind") not in ("fake", "claude", "command"):
        raise ValueError("backend.kind: expected fake, claude or command")
    if backend["kind"] == "fake":
        unknown = backend.keys() - {"kind", "usd", "reserve_usd", "failure", "responses"}
        if unknown:
            raise ValueError(f"backend: unknown fake options {sorted(unknown)}")
        if backend.get("usd", ".01") is not None:
            amount(backend.get("usd", ".01"))
        if amount(backend.get("reserve_usd", ".02")) <= 0:
            raise ValueError("backend.reserve_usd must be positive")
        if backend.get("failure") not in (None, "paid_error", "unknown", "not_started", "missing_cost"):
            raise ValueError("backend.failure: unsupported fake failure mode")
        if "responses" in backend and (not isinstance(backend["responses"], list) or
                                       any(not isinstance(x, str) for x in backend["responses"])):
            raise ValueError("backend.responses: expected a text array")
    else:
        from .backends import ClaudeCliBackend, CommandBackend
        adapter_class = CommandBackend if backend["kind"] == "command" else ClaudeCliBackend
        try:
            adapter = adapter_class(**{k: v for k, v in backend.items() if k != "kind"})
            for model in result["models"]:
                adapter.estimate_max_usd("", model, result["max_output_tokens"])
        except (TypeError, KeyError) as exc:
            raise ValueError(f"backend: invalid configuration: {exc}") from exc
    return result


def load_config(path):
    return validate_config(read_json(path))
