"""Read-only exam loading, including explicitly chosen private directories."""

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import re

from .budget import cli_boundary


EXAM_TYPES = ("human_baseline", "open", "forecast", "wrong_premise", "planted",
              "decomposable", "narrow", "injection", "real")
FIELDS = {"id", "type", "question", "constraints", "cards", "planted", "hidden_answers",
          "wrong_premise", "created_after", "leak_check", "license"}


def safe_segment(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise ValueError(f"Unsafe path identifier: {value!r}")
    return value


def read_json(path, *, parse_float=float):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), parse_float=parse_float)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc


def read_jsonl(path):
    text = Path(path).read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        raise ValueError(f"{path}: truncated final line")
    rows = []
    for line, content in enumerate(text.split("\n")[:-1], 1):
        try:
            row = json.loads(content, parse_float=Decimal)
            if not isinstance(row, dict):
                raise ValueError("expected an object")
            rows.append(row)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"{path}: line {line}: {exc}") from exc
    return rows


def text_list(value, field):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"{field}: expected a list of nonempty strings")


def validate_exam(value, cards):
    if not isinstance(value, dict) or FIELDS - value.keys():
        raise ValueError(f"exam missing fields: {sorted(FIELDS - value.keys()) if isinstance(value, dict) else 'object'}")
    if value.keys() - (FIELDS | {"check_samples"}):
        raise ValueError("exam: unknown fields")
    safe_segment(value["id"])
    if value["type"] not in EXAM_TYPES:
        raise ValueError("type: unknown exam type")
    for key in ("question", "cards", "license"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"{key}: expected nonempty text")
    for key in ("constraints", "hidden_answers", "check_samples"):
        text_list(value.get(key, []), key)
    for key in ("wrong_premise", "created_after", "leak_check"):
        if value[key] is not None and (not isinstance(value[key], str) or not value[key].strip()):
            raise ValueError(f"{key}: expected text or null")
    if value["type"] == "wrong_premise" and value["wrong_premise"] is None:
        raise ValueError("wrong_premise: this exam type requires the hidden premise")
    if value["type"] == "forecast" or value["created_after"] is not None:
        try:
            date.fromisoformat(value["created_after"])
        except (ValueError, TypeError):
            raise ValueError("created_after: expected an ISO date") from None
    ids = set()
    for number, card in enumerate(cards, 1):
        if not isinstance(card, dict) or set(card) != {"id", "text"}:
            raise ValueError(f"cards line {number}: expected id/text")
        safe_segment(card["id"])
        if card["id"] in ids or not isinstance(card["text"], str) or not card["text"].strip():
            raise ValueError(f"cards line {number}: duplicate id or empty text")
        ids.add(card["id"])
    if not isinstance(value["planted"], list):
        raise ValueError("planted: expected a list")
    for probe in value["planted"]:
        if (not isinstance(probe, dict) or set(probe) != {"card_id", "kind", "marker"}
                or not isinstance(probe["card_id"], str) or probe["card_id"] not in ids
                or probe["kind"] not in ("color", "injection")
                or not isinstance(probe["marker"], str) or not probe["marker"].strip()):
            raise ValueError("planted: invalid card reference, kind or marker")
        if not any(card["id"] == probe["card_id"] and probe["marker"] in card["text"] for card in cards):
            raise ValueError("planted: marker is absent from the referenced card")
    expected_probe = {"injection": "injection", "planted": "color"}.get(value["type"])
    if expected_probe and not any(probe["kind"] == expected_probe for probe in value["planted"]):
        raise ValueError(f"planted: {value['type']} requires a {expected_probe} probe")
    return {**value, "card_data": cards}


def load_exams(exam_dir):
    root = Path(exam_dir).resolve(strict=True)
    paths = sorted(root.rglob("exam.json"))
    if not paths:
        raise ValueError(f"{root}: no exam.json files")
    exams, ids = [], set()
    for path in paths:
        try:
            path.resolve(strict=True).relative_to(root)
            value = read_json(path)
            if not isinstance(value, dict) or not isinstance(value.get("cards"), str):
                raise ValueError("cards: missing relative path")
            relative = Path(value["cards"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("cards: path must stay inside exam directory")
            card_path = (path.parent / relative).resolve(strict=True)
            card_path.relative_to(root)
            exam = validate_exam(value, read_jsonl(card_path))
            if exam["id"] in ids:
                raise ValueError(f"id: duplicate exam {exam['id']}")
            ids.add(exam["id"])
            exams.append(exam)
        except (ValueError, OSError) as exc:
            raise ValueError(f"{path}: {exc}") from exc
    return exams


def register(subparsers):
    parser = subparsers.add_parser("validate_exams", help="Validate public or private exam files")
    parser.add_argument("--exam-dir", type=Path, required=True)
    parser.set_defaults(func=lambda args: cli_boundary(
        lambda args: print(f"Validated {len(load_exams(args.exam_dir))} exams"), args))
