"""Blind review sheets: selection, mandatory rewriting, conservation, answers and reveal."""

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import re
import secrets

from .backends import NotStarted
from .budget import BudgetLedger, LedgerError, atomic_write, cli_boundary, default_ledger_path, dumps, guarded_call
from .config import LEVELS, load_config, positive_int
from .dedupe import checked_ideas, validate_table
from .exam import load_exams, read_json, safe_segment
from .judge import check_exam, safe_output, stage_backend
from .metrics import load_unit, unit_completion
from .prompts import wrap_data
from . import replacements
from .record import MeasuredBackend, add_failure, digest, read_file, read_rows, recording_lock, sync_call


RESERVED_EXAMS = {"sheets", "replay"}
KEY_NAME = "sheet.key.json"
ISSUED_NAME = "sheet.issued.json"
ANSWER_FIELDS = {"unthought", "care", "fatal_reason", "want_to_try"}
VERDICT_FIELDS = {"exam_id", "mechanism_id", "label", "idea_ids", "sheet_id", "ts"} | ANSWER_FIELDS
ITEM_FIELDS = {"item_id", "text", "variants"}
SHEET_FIELDS = {"sheet_id", "kind", "question", "constraints", "items"}
OLD_SHEET_FIELDS = {"sheet_id", "kind", "items"}  # Issued before the page showed the question.
DEFAULT_COMPARE_COUNT = 40
DEFAULT_GROUP_COUNT = 30
MIN_UNIT_SAMPLE = 10
MASK = "[已隐去]"
SEPARATOR = r"[\s_-]"
# One-to-one folds, so a match position in the folded text is the same position in the original.
# Not NFKC: that would also change the full-width punctuation of the body. The first fold keeps full-width
# letters, so that ASCII written next to them ("ｘc1ｙ") still has a boundary.
FOLD_MARKS = str.maketrans({**dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d", "-"),
                            "\uff3f": "_", "\u3000": " "})
FOLD_ALL = {**FOLD_MARKS, **{code: code - 0xfee0 for code in range(0xff01, 0xff5f)}}


def bounded(core):
    """The one boundary rule for source names and identifiers: only an ASCII letter or digit extends a word.

    Never \\b, which treats Chinese as word characters and misses "编号m-001". "-" and "_" never shield a value,
    so "c1-方法" and "ref-m-001" match; "c1_b" is cut too, which is the safer mistake for a blind review.
    """
    return re.compile(rf"(?<![A-Za-z0-9])(?:{core})(?![A-Za-z0-9])", re.IGNORECASE)


IDENTIFIER = bounded(r"m-[0-9]{3,}|i-[0-9a-f]{12}")
REPEAT_SHARE = 4  # Repeats never exceed one quarter of the presented targets.
# ceiling: rejection sampling suits sheets where repeats stay under a quarter; place items directly if it ever fails.
ORDER_ATTEMPTS = 1000


class AnswerError(ValueError):
    """A rejected answer; the message is shown on the page, so it never names lineage."""


class SheetClosed(ValueError):
    """The sheet was revealed; further answers cannot change what was already merged."""


def unique_object(pairs):
    value = {}
    for key, entry in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = entry
    return value


def decode_payload(raw):
    """Strict request decoding: duplicate fields would silently overwrite an answer."""
    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, RecursionError) as exc:
        raise AnswerError("提交的内容不是有效的 JSON，请刷新页面再试") from exc


def sheet_path(run_dir, sheet_id):
    root = Path(run_dir).resolve()
    return safe_output(root / "sheets" / safe_segment(sheet_id), ())


def idea_text(row):
    return f"{row['title']}\n{row['text']}"


def unit_name(config, rep):
    return f"{config}/rep-{rep}"


def load_ideas(run_dir, exam_id):
    """Read every recorded unit of one exam; replay partitions live elsewhere and are never read."""
    root = Path(run_dir).resolve() / exam_id
    rows = []
    for path in sorted(root.glob("*/rep-*/ideas.jsonl")):
        source = read_rows(path)
        if any((r.get("exam_id"), r.get("config"), f"rep-{r.get('rep')}") != (exam_id, path.parent.parent.name, path.parent.name)
               for r in source):
            raise ValueError("Recording file disagrees with its unit directory")
        rows.extend(source)
    retired = {(row["config"], row["failed_rep"]) for row in replacements.load(run_dir, exam_id)}
    if retired and (root / "mechanisms.json").is_file():
        # A replaced unit's ideas that were never grouped stay on disk and in the report's count, not in the table's universe.
        grouped = {key for row in read_file(root / "mechanisms.json")["mechanisms"] for key in row["idea_ids"]}
        rows = [row for row in rows if (row.get("config"), row.get("rep")) not in retired or row.get("idea_id") in grouped]
    return checked_ideas(rows, exam_id)


def load_table(run_dir, exam_id, ideas):
    path = Path(run_dir).resolve() / exam_id / "mechanisms.json"
    if not path.is_file():
        raise ValueError("Authoritative mechanisms.json is missing; run dedupe first")
    table = read_file(path)
    try:
        validate_table(table, ideas, exam_id)
    except ValueError as exc:
        members = {key for row in table.get("mechanisms", []) for key in row.get("idea_ids", [])} if isinstance(table, dict) else set()
        if set(ideas) - members:
            raise ValueError(f"{exc}: {len(set(ideas) - members)} recorded ideas are not grouped yet. Finish their unit, "
                             "or if it failed, run replace-unit") from exc
        raise
    return table


def load_verdicts(run_dir, exam_id, table):
    """Reuse only checked verdicts: a mismatch is an error, never a silently applied answer."""
    current = {row["mechanism_id"]: row for row in table["mechanisms"]}
    verdicts = {}
    for row in read_rows(Path(run_dir).resolve() / exam_id / "verdicts.jsonl"):
        if set(row) != VERDICT_FIELDS or row["exam_id"] != exam_id or not valid_answer(row):
            raise ValueError("Invalid verdict row")
        if not isinstance(row["mechanism_id"], str):
            raise ValueError("Invalid verdict row")
        mechanism = current.get(row["mechanism_id"])
        if mechanism is None or row["mechanism_id"] in verdicts:
            raise ValueError(f"Verdict names an unknown or repeated mechanism: {row['mechanism_id']}")
        if (not isinstance(row["idea_ids"], list) or not row["idea_ids"]
                or any(not isinstance(key, str) for key in row["idea_ids"])
                or not set(row["idea_ids"]) <= set(mechanism["idea_ids"])):
            raise ValueError(f"Verdict idea_ids are not a subset of mechanism {row['mechanism_id']}")
        verdicts[row["mechanism_id"]] = row
    return verdicts


def valid_answer(row):
    reason = row["fatal_reason"]
    return (type(row["unthought"]) is bool and type(row["care"]) is bool and type(row["want_to_try"]) is bool
            and (reason is None or (isinstance(reason, str) and bool(reason.strip()))))


def scored(answer):
    return bool(answer["unthought"] and answer["care"] and not answer["fatal_reason"])


def lineage_terms(ideas, exam, configs):
    # "baseline" and "representative" are left out as ordinary words: only check_sheet_id and the response
    # shape guard them. A separator before the number makes "pressure 1", "pressure-1" and "pressure1" one term.
    levels = {re.sub(r"(\d)$", r" \1", level) for level in LEVELS if level != "baseline"}
    terms = {"config": set(configs), "model": set(), "method": set(), "card": set(), "level": levels,
             "field": {"idea_id", "mechanism_id", "config_reps"}}
    for row in ideas.values():
        terms["config"].add(row["config"])
        terms["model"].add(row["model"])
        if row["method"] is not None:
            terms["method"].add(row["method"])
        terms["card"].update(row["cards"], row["cards_used"])
    terms["card"].update(card["id"] for card in exam.get("card_data", []))
    return terms


def term_patterns(terms):
    """The one scanner behind rewrite refusal, masking, warnings and sheet ids. Separators inside a name are
    interchangeable. Best effort: it only knows these values, see the known limits in the handoff."""
    patterns = []
    for kind, values in sorted(terms.items()):
        for term in sorted(values):
            parts = [re.escape(part) for part in re.split(SEPARATOR, term.translate(FOLD_ALL)) if part]
            if parts:  # A blank name would match everywhere.
                patterns.append((kind, bounded(f"{SEPARATOR}*".join(parts))))
    return patterns + [("identifier", IDENTIFIER)]


def lineage_spans(text, patterns):
    return [(match.start(), match.end(), kind) for folded in {text.translate(FOLD_MARKS), text.translate(FOLD_ALL)}
            for kind, pattern in patterns for match in pattern.finditer(folded)]


def lineage_hits(text, patterns):
    return sorted({kind for _, _, kind in lineage_spans(text, patterns)})


def mask_wordings(shown, variants, patterns):
    """The full and group views are not rewritten, so source terms are cut out instead of reaching the page."""
    def mask(text):
        # Overlaps merge, so a longer name is never left half cut by a shorter name inside it.
        result, cursor = "", 0
        for start, end, _ in sorted(lineage_spans(text, patterns)):
            if start >= cursor:
                result += text[cursor:start] + MASK
            cursor = max(cursor, end)
        return result + text[cursor:]
    shown = mask(shown)
    return shown, [text for text in dict.fromkeys(mask(text) for text in variants) if text != shown]


def check_sheet_id(sheet_id, terms):
    """The sheet id reaches the page, so it obeys the same rules as the text on it."""
    squeeze = lambda text: re.sub(SEPARATOR, "", text).lower()
    # Stricter than for text: an id has no reason to hold a source name even inside a longer word.
    # Short names only count as whole words, or the config "a" would refuse every id.
    inside = any(len(squeeze(term)) >= 3 and squeeze(term) in squeeze(sheet_id) for values in terms.values() for term in values)
    if inside or lineage_hits(sheet_id, term_patterns({**terms, "baseline": {"baseline"}})):
        raise ValueError("sheet_id must not contain a source name or an internal identifier")


def discover_units(run_dir, exam_id, config):
    safe_segment(config)
    root = Path(run_dir).resolve() / exam_id / config
    matches = (re.fullmatch(r"rep-([1-9][0-9]*)", path.name) for path in root.glob("rep-*"))
    retired = replacements.retired_reps(replacements.load(run_dir, exam_id), config)
    reps = sorted(int(match.group(1)) for match in matches if match and int(match.group(1)) not in retired)
    if not reps:
        raise ValueError(f"Config {config} has no recorded units")
    return [(config, rep) for rep in reps]


def unit_mechanisms(table, ideas, unit, *, include_baseline):
    # ceiling: scans every member once per unit; group ideas by unit first if exams reach many thousands of ideas.
    return {row["mechanism_id"] for row in table["mechanisms"]
            if any((ideas[key]["config"], ideas[key]["rep"]) == unit
                   and (include_baseline or ideas[key]["level"] != "baseline") for key in row["idea_ids"])}


def select_compare(table, ideas, units, verdicts, *, seed, count, per_unit):
    """All units share one priority order, so each sample is a uniform subset with maximal overlap."""
    members = {unit: unit_mechanisms(table, ideas, unit, include_baseline=False) for unit in units}
    priority = sorted(row["mechanism_id"] for row in table["mechanisms"])
    random.Random(f"{seed}:priority").shuffle(priority)
    ranked = {unit: [key for key in priority if key in members[unit]] for unit in units}
    smallest = min(len(rows) for rows in ranked.values())
    if smallest == 0:
        raise ValueError("A compared unit has no non-baseline mechanisms")
    # ceiling: rebuilds the union for each candidate k, which is fine for hundreds of mechanisms per exam.
    def union(k):
        return {key for rows in ranked.values() for key in rows[:k]}
    if per_unit is not None:
        positive_int(per_unit, "per_unit")
        if per_unit > smallest:
            raise ValueError(f"--per-unit {per_unit} exceeds the smallest unit size {smallest}")
        k = per_unit
    else:
        positive_int(count, "count")
        k = 0
        for candidate in range(1, smallest + 1):
            if len(union(candidate) - set(verdicts)) > count:
                break
            k = candidate
        if k == 0:
            raise ValueError(f"--count {count} cannot hold one item per unit")
    return k, {unit_name(*unit): rows[:k] for unit, rows in ranked.items()}


def presentable(selected, verdicts):
    """Mechanisms the author still has to answer; separate so conservation can catch a dropped row."""
    return [key for key in selected if key not in verdicts]


def check_conservation(view, items, counts, reused):
    """Both issue and reveal verify this: an unequal sheet is an error, not a warning."""
    if view not in ("all", "compare", "group"):
        raise ValueError("Conservation failed: unknown sheet view")
    types = Counter(item["type"] for item in items.values())
    if (types["target"] != counts["presented_target"] or types["check_bad"] != counts["check_bad"]
            or types["repeat"] != counts["repeat"] or len(reused) != counts["reused"]
            or set(types) - {"target", "check_bad", "repeat"}):
        raise ValueError("Conservation failed: item types disagree with counts")
    if counts["presented_target"] + counts["reused"] != counts["selected_target"]:
        raise ValueError("Conservation failed: presented_target + reused != selected_target")
    if view == "all" and counts["selected_target"] != counts["mechanisms_total"]:
        raise ValueError("Conservation failed: selected_target != mechanisms_total")
    if view != "group":
        targets = [item["mechanism_id"] for item in items.values() if item["type"] == "target"]
        if len(set(targets)) != len(targets) or set(targets) & set(reused):
            raise ValueError("Conservation failed: a mechanism is presented twice")


def rewrite_prompt(question, rows):
    return "\n".join([
        "Rewrite every item below into one plain, uniform style.",
        "Keep each item's mechanism and every point. Do not add, remove or evaluate anything.",
        "Do not mention where an item came from, how it was produced, or any identifiers inside the text.",
        "Write each item as a single paragraph, with no line breaks and no separate title line.",
        'Return only JSON: {"rewrites":[{"item":"r-0","text":"..."}]}, covering every item exactly once.',
        "Question:", wrap_data(question), "Items:", wrap_data(rows)])


def fake_rewrite(prompt, model, max_output_tokens):
    """Deterministic wiring fixture, not evidence of rewriting quality."""
    block = prompt.rsplit("\nItems:\n", 1)[1].split("\n")
    rows = json.loads("\n".join(block[2:-1]))
    return dumps({"rewrites": [{"item": row["item"], "text": "平实版：" + row["text"]} for row in rows]})


def parse_rewrites(text, names):
    try:
        data = json.loads(text, object_pairs_hook=unique_object)
        if not isinstance(data, dict) or set(data) != {"rewrites"} or not isinstance(data["rewrites"], list):
            raise ValueError("Invalid envelope")
        result = {}
        for row in data["rewrites"]:
            if (not isinstance(row, dict) or set(row) != {"item", "text"} or row["item"] not in names
                    or row["item"] in result or not isinstance(row["text"], str) or not row["text"].strip()):
                raise ValueError("Invalid row")
            # Enforced here too: a check sample must not stand out by a structure the model happened to keep.
            result[row["item"]] = " ".join(row["text"].split())
        if set(result) != set(names):
            raise ValueError("Missing items")
        return result
    except (ValueError, TypeError, RecursionError) as exc:
        # Never echo model text: the author is still blind and it may quote private material.
        raise ValueError("Invalid rewrite response; every item must be returned exactly once") from exc


def rewrite_batches(slots, *, seed, batch_size):
    """Mix check samples with targets; repeats go alone so they cannot copy the original's wording."""
    positive_int(batch_size, "rewrite_batch")
    mixed = [slot for slot in slots if slot["type"] != "repeat"]
    random.Random(f"{seed}:batches").shuffle(mixed)
    repeats = [slot for slot in slots if slot["type"] == "repeat"]
    return [group[i:i + batch_size] for group in (mixed, repeats) for i in range(0, len(group), batch_size)]


def save_rewrite_state(path, state):
    atomic_write(path / "rewrite_state.json", dumps({"digest": digest(state), "state": state}) + "\n")


def export_rewrite(path, state):
    save_rewrite_state(path, state)
    for name in ("calls", "failures"):
        atomic_write(path / f"{name}.jsonl", "".join(dumps(row) + "\n" for row in state[name]))


def rewrite_all(path, exam, slots, identity, *, sheet_id, seed, backend, ledger, model, batch_size,
                attempt, max_output_tokens, patterns):
    """Every presented text passes the ledger. Rerunning replays cached results; only a new attempt pays."""
    state_path = path / "rewrite_state.json"
    if state_path.exists():
        envelope = read_file(state_path)
        state = envelope["state"]
        if envelope["digest"] != digest(state) or digest(state["identity"]) != digest(identity):
            raise ValueError("Rewrite state conflicts with this sheet's inputs")
    else:
        state = {"identity": identity, "requests": {}, "calls": [], "failures": []}
    markers = [probe["marker"] for probe in exam.get("planted", []) if probe["kind"] == "injection"]
    question = exam["question"]
    result = {}
    def fail(key, reason, detail):
        add_failure(state, "rewrite", key, reason, detail)
        export_rewrite(path, state)
    for number, batch in enumerate(rewrite_batches(slots, seed=seed, batch_size=batch_size)):
        names = {f"r-{i}": slot for i, slot in enumerate(batch)}
        prompt = rewrite_prompt(question, [{"item": name, "text": slot["original"]} for name, slot in names.items()])
        key = "rewrite_" + digest([exam["id"], sheet_id, number, digest(prompt), model, backend.name, max_output_tokens, attempt])
        request = state["requests"].setdefault(key, {
            "call_key": key, "exam_id": exam["id"], "config": "sheet", "rep": 1, "agent": number + 1,
            "level": "round1", "purpose": "rewrite", "backend": backend.name, "model": model, "attempt": attempt,
            "started_at": None, "duration_ms": None})
        save_rewrite_state(path, state)
        measured = MeasuredBackend(backend, request, lambda: save_rewrite_state(path, state))
        try:
            response = guarded_call(ledger, measured, key, prompt, model, max_output_tokens)
        except (LedgerError, NotStarted) as exc:
            sync_call(state, key, ledger)
            fail(key, type(exc).__name__, "Rewrite call did not produce a usable result")
            raise
        if measured.persistence_error is not None:
            raise measured.persistence_error
        sync_call(state, key, ledger)
        try:
            texts = parse_rewrites(response.text, names)
        except ValueError as exc:
            fail(key, "invalid_response", "Every item must be returned exactly once")
            raise ValueError(f"{exc}; rerun with a higher --rewrite-attempt to pay for a retry") from exc
        for name, slot in names.items():
            hits = lineage_hits(texts[name], patterns)
            if hits:
                fail(key, "lineage_term", f"Rewritten text names: {','.join(hits)}")
                raise ValueError(f"Rewritten text names a source term ({','.join(hits)}); sheet refused")
            if any(marker in texts[name] and marker not in slot["original"] for marker in markers):
                fail(key, "injection_marker", "Rewritten text introduced an injection marker")
                raise ValueError("Rewritten text introduced an injection marker; sheet refused")
            result[slot["position"]] = texts[name]
    state["failures"] = []
    export_rewrite(path, state)
    return result


def open_score_sheet(run_dir, exam_id, sheet_id):
    """One open score sheet per exam, or two sheets could reveal conflicting verdicts."""
    # ceiling: reads every key per issue and allows one open sheet; add per-mechanism claims if parallel sheets are needed.
    root = Path(run_dir).resolve() / "sheets"
    for path in sorted(root.glob(f"*/{KEY_NAME}")):
        if path.parent.name == sheet_id or (path.parent / "revealed.json").exists():
            continue
        key = read_file(path)
        if key.get("kind") == "score" and key.get("exam_id") == exam_id:
            return path.parent.name
    return None


def order_items(slots, seed):
    """Seeded shuffle. A target comes before its repeat, because question one is scored from the target
    and only a first viewing can answer it; the two are never adjacent, or copying would be effortless."""
    rng = random.Random(f"{seed}:order")
    # Only a target and its repeat share a mechanism; check samples have none.
    mechanism = {slot["position"]: slot["mechanism_id"] for slot in slots}
    order = [slot["position"] for slot in slots]
    targets = {slot["mechanism_id"]: slot["position"] for slot in slots if slot["type"] == "target"}
    pairs = [(targets[slot["mechanism_id"]], slot["position"]) for slot in slots if slot["type"] == "repeat"]
    for _ in range(ORDER_ATTEMPTS):
        rng.shuffle(order)
        for target, repeat in pairs:
            first, second = order.index(target), order.index(repeat)
            if first > second:
                order[first], order[second] = order[second], order[first]
        if not any(mechanism[a] is not None and mechanism[a] == mechanism[b] for a, b in zip(order, order[1:])):
            return order
    raise ValueError("Could not keep repeats away from their originals; lower --repeats")


def target_slots(table, ideas, presented, *, seed, all_view, repeats):
    """Build target and repeat slots. In the full view a repeat uses another wording whenever one exists."""
    by_id = {row["mechanism_id"]: row for row in table["mechanisms"]}
    slots = []
    def add(kind, mechanism_id, text, others):
        variants = sorted(set(others) - {text}) if all_view else []
        random.Random(f"{seed}:variants:{mechanism_id}:{kind}").shuffle(variants)
        row = by_id[mechanism_id]
        slots.append({"position": len(slots), "type": kind, "mechanism_id": mechanism_id,
                      "idea_ids": row["idea_ids"], "original": text, "variants": variants})
    def wordings(mechanism_id):
        row = by_id[mechanism_id]
        return idea_text(ideas[row["representative"]]), [idea_text(ideas[key]) for key in row["idea_ids"]]
    for mechanism_id in presented:
        add("target", mechanism_id, *wordings(mechanism_id))
    actual = min(repeats, len(presented) // REPEAT_SHARE)
    chosen = sorted(presented)
    random.Random(f"{seed}:repeat").shuffle(chosen)
    if all_view:
        # Stable sort: mechanisms with another wording first, so a repeat is rarely an identical text.
        chosen.sort(key=lambda mechanism_id: len(set(wordings(mechanism_id)[1])) < 2)
    for mechanism_id in chosen[:actual]:
        shown, texts = wordings(mechanism_id)
        other = sorted(set(texts) - {shown}) if all_view else []
        add("repeat", mechanism_id, other[0] if other else shown, texts)
    return slots, actual


def make_sheet(run_dir, exam, *, kind, sheet_id, seed, all_view=False, configs=None, count=None, per_unit=None,
               check_samples=3, repeats=2, backend=None, ledger=None, model=None,
               rewrite_batch=10, rewrite_attempt=1, max_output_tokens=2048):
    if kind not in ("score", "group"):
        raise ValueError("kind must be score or group")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if not isinstance(exam, dict) or not isinstance(exam.get("id"), str):
        raise ValueError("Exam requires a string id")
    exam_id = safe_segment(exam["id"])
    if exam_id in RESERVED_EXAMS:
        raise ValueError(f"exam id {exam_id} collides with a reserved run directory")
    check_exam(exam)
    if any(type(value) is not int or value < 0 for value in (check_samples, repeats)):
        raise ValueError("check_samples and repeats must be non-negative integers")
    path = sheet_path(run_dir, sheet_id)
    # One issuer per run directory: the open-sheet check and the key write must not interleave.
    # ceiling: held across paid rewrites, so a concurrent issue or reveal waits; split by exam if that wait matters.
    with recording_lock(path.parent):
        if (path / "sheet.json").exists():
            raise ValueError(f"Sheet {sheet_id} was already issued")
        ideas = load_ideas(run_dir, exam_id)
        terms = lineage_terms(ideas, exam, configs or ())
        check_sheet_id(sheet_id, terms)
        warnings = []
        if kind == "group":
            sheet = group_plan(ideas, seed=seed, count=DEFAULT_GROUP_COUNT if count is None else count, warnings=warnings)
        else:
            sheet = score_plan(run_dir, exam, ideas, sheet_id=sheet_id, seed=seed, all_view=all_view, configs=configs,
                               count=DEFAULT_COMPARE_COUNT if count is None else count, per_unit=per_unit, check_samples=check_samples,
                               repeats=repeats, warnings=warnings)
        identity = {"kind": kind, "exam_id": exam_id, "seed": seed, "view": sheet["view"], "configs": configs,
                    "count": count, "per_unit": per_unit, "check_samples": check_samples, "repeats": repeats,
                    "sources": digest([sorted(ideas), sheet["source_digest"]])}
        slots = sheet["slots"]
        patterns = term_patterns(terms)
        # Shown as written: the same text sits above every item, so it cannot tell one item's source from another's.
        context_hits = lineage_hits("\n".join([exam["question"], *exam["constraints"]]), patterns)
        if context_hits:
            warnings.append(f"The exam question or constraints name a source term ({', '.join(context_hits)}); "
                            "shown as written, so reword the exam if that gives too much away")
        if sheet["view"] == "compare" and slots:
            if backend is None or ledger is None or not isinstance(model, str) or not model.strip():
                raise ValueError("Comparison sheets require a rewrite backend, ledger and model")
            positive_int(rewrite_attempt, "rewrite_attempt")
            positive_int(max_output_tokens, "max_output_tokens")
            texts = rewrite_all(path, exam, slots, {**identity, "model": model, "backend": backend.name,
                                                    "ledger": str(ledger.path), "rewrite_batch": rewrite_batch,
                                                    "max_output_tokens": max_output_tokens},
                                sheet_id=sheet_id, seed=seed, backend=backend, ledger=ledger, model=model,
                                batch_size=rewrite_batch, attempt=rewrite_attempt,
                                max_output_tokens=max_output_tokens, patterns=patterns)
        else:
            texts = {}
            flagged, kinds = 0, Counter()
            for slot in slots:
                hits = lineage_hits("\n".join([slot["original"], *slot["variants"]]), patterns)
                flagged += bool(hits)
                kinds.update(hits)
                slot["shown"], slot["variants"] = mask_wordings(slot["original"], slot["variants"], patterns)
            if flagged:
                detail = ", ".join(f"{kind}: {number}" for kind, number in sorted(kinds.items()))
                warnings.append(f"{flagged} items contain a source term ({detail}); masked on the page and kept in the key. "
                                "Ordinary words with the same name are masked too, so use distinctive source names")
        identity["attempt"] = rewrite_attempt if sheet["view"] == "compare" else None
        return issue(path, sheet, slots, texts, identity, exam=exam, kind=kind, sheet_id=sheet_id,
                     seed=seed, warnings=warnings)


def usable_samples(samples, slots, limit, all_view, warnings):
    """A sample must look real in every view: a title line like real items, because a kept title leaves a
    visible mark even after flattening. The full view also needs real items that lack variants."""
    titled = [text for text in samples if sum(bool(line.strip()) for line in text.splitlines()) > 1]
    if len(titled) < len(samples):
        warnings.append(f"{len(samples) - len(titled)} check samples have no title line and were left out; they would stand out")
    if not all_view:
        return titled
    room = sum(slot["type"] == "target" and not slot["variants"] for slot in slots)
    if min(limit, len(titled)) > room:
        warnings.append(f"Check samples limited to {room}: only that many real items are without other wordings")
    return titled[:room]


def group_plan(ideas, *, seed, count, warnings):
    positive_int(count, "count")
    if not ideas:
        raise ValueError("No recorded ideas to group")
    keys = sorted(ideas)
    random.Random(f"{seed}:group").shuffle(keys)
    if len(keys) < count:
        warnings.append(f"Only {len(keys)} ideas exist; requested {count}")
    chosen = keys[:count]
    slots = [{"position": i, "type": "target", "mechanism_id": None, "idea_ids": [key],
              "original": idea_text(ideas[key]), "variants": []} for i, key in enumerate(chosen)]
    # Calibration is a cold start: this view deliberately ignores any machine partition.
    return {"view": "group", "slots": slots, "selected": chosen, "reused": [], "units": {}, "config_reps": {},
            "source_digest": digest([ideas[key] for key in chosen]),
            "counts": {"mechanisms_total": None, "selected_target": len(chosen), "presented_target": len(chosen),
                       "reused": 0, "check_bad": 0, "repeat": 0}}


def score_plan(run_dir, exam, ideas, *, sheet_id, seed, all_view, configs, count, per_unit, check_samples, repeats, warnings):
    exam_id = exam["id"]
    table = load_table(run_dir, exam_id, ideas)
    verdicts = load_verdicts(run_dir, exam_id, table)
    other = open_score_sheet(run_dir, exam_id, sheet_id)
    if other is not None:
        raise ValueError(f"Sheet {other} for this exam has not been revealed yet")
    if all_view:
        if configs or per_unit is not None:
            raise ValueError("--all shows everything; it takes no --configs or --per-unit")
        units = sorted({(row["config"], row["rep"]) for row in ideas.values()})
        members = {unit: unit_mechanisms(table, ideas, unit, include_baseline=True) for unit in units}
        assignment = {unit_name(*unit): sorted(members[unit]) for unit in units}
        selected = [row["mechanism_id"] for row in table["mechanisms"]]
        incomplete = [unit for unit in units if unit_completion(load_unit(run_dir, exam_id, *unit))["status"] != "ok"]
        if incomplete:
            warnings.append(f"{len(incomplete)} recorded units are incomplete; their missing ideas cannot be shown")
    else:
        if not configs:
            raise ValueError("Comparison sheets require --configs")
        units = [unit for config in configs for unit in discover_units(run_dir, exam_id, config)]
        for unit in units:
            completion = unit_completion(load_unit(run_dir, exam_id, *unit))
            if completion["status"] != "ok":
                raise ValueError(f"Unit {unit_name(*unit)} is incomplete: {completion.get('reason')}")
        k, assignment = select_compare(table, ideas, units, verdicts, seed=seed, count=count, per_unit=per_unit)
        if k < MIN_UNIT_SAMPLE:
            warnings.append(f"Each unit has only {k} items, below the {MIN_UNIT_SAMPLE} sample warning line")
        selected = sorted({key for rows in assignment.values() for key in rows})
    presented = presentable(selected, verdicts)
    reused = sorted(set(selected) & set(verdicts))
    slots, actual_repeats = target_slots(table, ideas, presented, seed=seed, all_view=all_view, repeats=repeats)
    if actual_repeats < repeats:
        warnings.append(f"Repeats reduced to {actual_repeats}; requested {repeats}")
    samples = sorted(set(exam.get("check_samples", [])))
    random.Random(f"{seed}:check").shuffle(samples)
    if not presented:
        warnings.append("Nothing new to present; every selected mechanism already has a verdict")
    elif not samples and check_samples:
        warnings.append("This exam has no check_samples; check_bad is 0")
    elif check_samples:
        samples = usable_samples(samples, slots, check_samples, all_view, warnings)
    chosen = samples[:check_samples] if presented else []
    for text in chosen:
        slots.append({"position": len(slots), "type": "check_bad", "mechanism_id": None, "idea_ids": [],
                      "original": text, "variants": []})
    # Attribution is every producing unit; `units` separately records which slots each unit is scored on.
    config_reps = {row["mechanism_id"]: sorted({unit_name(ideas[key]["config"], ideas[key]["rep"]) for key in row["idea_ids"]})
                   for row in table["mechanisms"]}
    view = "all" if all_view else "compare"
    return {"view": view, "slots": slots, "selected": selected, "reused": reused, "units": assignment,
            "config_reps": config_reps,
            "source_digest": digest([table, sorted(verdicts)]),
            "counts": {"mechanisms_total": len(table["mechanisms"]), "selected_target": len(selected),
                       "presented_target": sum(slot["type"] == "target" for slot in slots),
                       "reused": len(reused), "check_bad": len(chosen), "repeat": actual_repeats}}


def issued_record(sheet, items, *, sheet_id, exam_id):
    """The complete ID sets of one sheet, fixed when it is issued and never rewritten for that sheet."""
    return {"sheet_id": sheet_id, "exam_id": exam_id, "view": sheet["view"], "selected": sorted(sheet["selected"]),
            "reused": sorted(sheet["reused"]), "units": sheet["units"], "counts": sheet["counts"],
            "items": {item_id: {"type": item["type"], "mechanism_id": item["mechanism_id"]} for item_id, item in items.items()}}


def load_issued(path):
    file = Path(path) / ISSUED_NAME
    if not file.is_file():
        raise ValueError(f"Conservation failed: {Path(path).name} has no issued record")
    return read_file(file)


def check_issued(record, key, public_ids):
    """Set equality against the issued record: a row lost later cannot hide by lowering the counts to match."""
    name = record["sheet_id"]
    items = {item_id: {"type": item["type"], "mechanism_id": item["mechanism_id"]} for item_id, item in key["items"].items()}
    if items != record["items"] or set(public_ids) != set(record["items"]) or len(public_ids) != len(record["items"]):
        raise ValueError(f"Conservation failed: {name} items differ from its issued record")
    if sorted(key["reused"]) != record["reused"] or key["units"] != record["units"] or key["counts"] != record["counts"]:
        raise ValueError(f"Conservation failed: {name} reused, units or counts differ from its issued record")
    if record["view"] != "group":
        targets = {item["mechanism_id"] for item in record["items"].values() if item["type"] == "target"}
        if targets | set(record["reused"]) != set(record["selected"]):
            raise ValueError(f"Conservation failed: {name} presented and reused mechanisms differ from those selected")


def issue(path, sheet, slots, texts, identity, *, exam, kind, sheet_id, seed, warnings):
    """Key first, sheet.json last: the sheet file is the commit point the service can open."""
    exam_id = exam["id"]
    key_path = path / KEY_NAME
    fingerprint = digest(identity)
    existing = None
    if key_path.exists():
        existing = read_file(key_path)
        if existing.get("identity") != fingerprint or len(existing.get("items", {})) != len(slots):
            raise ValueError("An unfinished sheet with this id has different inputs")
    ids = list(existing["items"]) if existing else [f"x-{secrets.token_hex(8)}" for _ in slots]
    order = order_items(slots, seed)
    items, public = {}, []
    for item_id, position in zip(ids, order):
        slot = slots[position]
        rewritten = texts.get(position)
        items[item_id] = {"type": slot["type"], "mechanism_id": slot["mechanism_id"], "idea_ids": slot["idea_ids"],
                          "original_text": slot["original"], "rewritten_text": rewritten,
                          "config_reps": sheet["config_reps"].get(slot["mechanism_id"], [])}
        public.append({"item_id": item_id, "text": rewritten if rewritten is not None else slot["shown"],
                       "variants": slot["variants"]})
    check_conservation(sheet["view"], items, sheet["counts"], sheet["reused"])
    key = {"sheet_id": sheet_id, "kind": kind, "exam_id": exam_id, "seed": seed, "view": sheet["view"],
           "identity": fingerprint, "items": items, "reused": sheet["reused"], "units": sheet["units"],
           "counts": sheet["counts"]}
    record = issued_record(sheet, items, sheet_id=sheet_id, exam_id=exam_id)
    check_issued(record, key, [row["item_id"] for row in public])
    atomic_write(path / ISSUED_NAME, dumps(record) + "\n")
    atomic_write(key_path, dumps(key) + "\n")
    atomic_write(path / "sheet.json", dumps({"sheet_id": sheet_id, "kind": kind, "question": exam["question"],
                                             "constraints": exam["constraints"], "items": public}) + "\n")
    return {"sheet_id": sheet_id, "counts": sheet["counts"], "warnings": warnings}


def valid_context(sheet):
    question, constraints = sheet["question"], sheet["constraints"]
    return (isinstance(question, str) and bool(question.strip()) and isinstance(constraints, list)
            and all(isinstance(text, str) and text.strip() for text in constraints))


def load_public_sheet(run_dir, sheet_id):
    """The only sheet reader the service uses. It validates the shape and never opens the key."""
    path = sheet_path(run_dir, sheet_id) / "sheet.json"
    if not path.is_file():
        raise ValueError(f"Sheet {sheet_id} does not exist")
    sheet = read_json(path)
    if isinstance(sheet, dict) and set(sheet) == OLD_SHEET_FIELDS:
        # Still readable: an unrevealed score sheet blocks the next one, so refusing it would be a dead end.
        sheet = {**sheet, "question": None, "constraints": []}
    elif not isinstance(sheet, dict) or set(sheet) != SHEET_FIELDS or not valid_context(sheet):
        raise ValueError("sheet.json has unexpected fields")
    if sheet["sheet_id"] != sheet_id or sheet["kind"] not in ("score", "group") or not isinstance(sheet["items"], list):
        raise ValueError("sheet.json has unexpected fields")
    seen = set()
    for item in sheet["items"]:
        if (not isinstance(item, dict) or set(item) != ITEM_FIELDS or not isinstance(item["item_id"], str)
                or item["item_id"] in seen or not isinstance(item["text"], str)
                or not isinstance(item["variants"], list) or any(not isinstance(text, str) for text in item["variants"])):
            raise ValueError("sheet.json has an unexpected item")
        seen.add(item["item_id"])
    return sheet


def load_answers(run_dir, sheet):
    path = sheet_path(run_dir, sheet["sheet_id"]) / "answers.json"
    known = {item["item_id"] for item in sheet["items"]}
    if not path.exists():
        return {} if sheet["kind"] == "score" else []
    data = read_json(path)
    field = "answers" if sheet["kind"] == "score" else "groups"
    if (not isinstance(data, dict) or set(data) != {"sheet_id", "kind", field}
            or data["sheet_id"] != sheet["sheet_id"] or data["kind"] != sheet["kind"]):
        raise ValueError("answers.json is corrupted")
    if sheet["kind"] == "score":
        answers = data["answers"]
        if (not isinstance(answers, dict) or set(answers) - known
                or any(not isinstance(row, dict) or set(row) != ANSWER_FIELDS or not valid_answer(row) for row in answers.values())):
            raise ValueError("answers.json is corrupted")
        return answers
    check_groups(data["groups"], known, ValueError)
    return data["groups"]


def check_groups(groups, known, error):
    seen = set()
    if not isinstance(groups, list):
        raise error("分组的格式不对，请刷新页面再试")
    for group in groups:
        if not isinstance(group, list) or not group:
            raise error("不能提交空的组")
        for item in group:
            if not isinstance(item, str) or item not in known:
                raise error("有一条点子不在这张卷子里，请刷新页面再试")
            if item in seen:
                raise error("同一条点子不能放进两个组")
            seen.add(item)


def progress(sheet, answers):
    if sheet["kind"] == "score":
        done = len(answers)
    else:
        done = sum(len(group) for group in answers)
    return {"done": done, "total": len(sheet["items"])}


def public_state(run_dir, sheet_id):
    sheet = load_public_sheet(run_dir, sheet_id)
    answers = load_answers(run_dir, sheet)
    return {"sheet_id": sheet_id, "kind": sheet["kind"], "question": sheet["question"],
            "constraints": sheet["constraints"], "items": sheet["items"],
            "answers": answers if sheet["kind"] == "score" else {"groups": answers},
            "progress": progress(sheet, answers)}


def merge_answers(run_dir, sheet_id, payload):
    """Validate the whole batch, merge under the lock, and return only after the write is durable."""
    sheet = load_public_sheet(run_dir, sheet_id)
    path = sheet_path(run_dir, sheet_id)
    known = {item["item_id"] for item in sheet["items"]}
    field = "answers" if sheet["kind"] == "score" else "groups"
    if not isinstance(payload, dict) or set(payload) != {"sheet_id", field}:
        raise AnswerError("提交的内容格式不对，请刷新页面再试")
    if payload["sheet_id"] != sheet_id:
        raise AnswerError("这不是当前这张卷子的答案，请刷新页面再试")
    if sheet["kind"] == "score":
        rows = payload["answers"]
        if not isinstance(rows, list):
            raise AnswerError("提交的内容格式不对，请刷新页面再试")
        update = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != ANSWER_FIELDS | {"item_id"}:
                raise AnswerError("这一条还没答完")
            if not isinstance(row["item_id"], str) or row["item_id"] not in known:
                raise AnswerError("有一条点子不在这张卷子里，请刷新页面再试")
            if row["item_id"] in update:
                raise AnswerError("同一条点子提交了两次，请刷新页面再试")
            if not valid_answer(row):
                raise AnswerError("这一条还没答完；说得出原因的话，要写一句")
            update[row["item_id"]] = {key: row[key] for key in sorted(ANSWER_FIELDS)}
    else:
        check_groups(payload["groups"], known, AnswerError)
    # ceiling: rewrites the whole file per save, which is fine for sheets of hundreds of items.
    with recording_lock(path):
        if (path / "revealed.json").exists():
            raise SheetClosed("这张卷子已经还原，不能再改了")
        if sheet["kind"] == "score":
            answers = {**load_answers(run_dir, sheet), **update}
        else:
            answers = payload["groups"]
        # Not budget.dumps: this private file stays readable when the author opens it, and holds no amounts.
        atomic_write(path / "answers.json", json.dumps({"sheet_id": sheet_id, "kind": sheet["kind"], field: answers},
                                                       ensure_ascii=False) + "\n")
    return {"ok": True, **progress(sheet, answers)}


def load_key(run_dir, sheet):
    path = sheet_path(run_dir, sheet["sheet_id"]) / KEY_NAME
    if not path.is_file():
        raise ValueError("sheet.key.json is missing; this sheet cannot be revealed")
    key = read_file(path)
    if (not isinstance(key, dict) or key.get("sheet_id") != sheet["sheet_id"] or key.get("kind") != sheet["kind"]
            or not isinstance(key.get("items"), dict) or set(key["items"]) != {item["item_id"] for item in sheet["items"]}):
        raise ValueError("sheet.key.json does not match sheet.json")
    return key


def verdict_rows(table, key, answers):
    exam_id = key["exam_id"]
    current = {row["mechanism_id"]: row for row in table["mechanisms"]}
    rows = []
    for item_id, item in key["items"].items():
        if item["type"] != "target":
            continue
        mechanism = current.get(item["mechanism_id"])
        if mechanism is None or not set(item["idea_ids"]) <= set(mechanism["idea_ids"]):
            raise ValueError(f"Conservation failed: key item disagrees with mechanism {item['mechanism_id']}")
        answer = answers[item_id]
        rows.append({"exam_id": exam_id, "mechanism_id": item["mechanism_id"], "label": mechanism["label"],
                     "idea_ids": item["idea_ids"], "unthought": answer["unthought"], "care": answer["care"],
                     "fatal_reason": answer["fatal_reason"], "want_to_try": answer["want_to_try"],
                     "sheet_id": key["sheet_id"]})
    for mechanism_id in key["reused"]:
        if mechanism_id not in current:
            raise ValueError(f"Conservation failed: reused mechanism {mechanism_id} is gone")
    return rows


def write_verdicts(run_dir, key, rows):
    """All-or-nothing rewrite. A crash before revealed.json reruns cleanly and only adds what is missing."""
    path = Path(run_dir).resolve() / key["exam_id"] / "verdicts.jsonl"
    existing = read_rows(path)
    known = {row["mechanism_id"]: row for row in existing}
    missing = [mechanism for mechanism in key["reused"] if mechanism not in known]
    if missing:
        raise ValueError(f"Conservation failed: reused mechanisms have no verdict: {missing}")
    now = datetime.now(timezone.utc).isoformat()
    additions = []
    for row in rows:
        old = known.get(row["mechanism_id"])
        if old is None:
            additions.append({**row, "ts": now})
        elif {k: v for k, v in old.items() if k != "ts"} != row:
            raise ValueError(f"Mechanism {row['mechanism_id']} already has a different verdict")
    if additions:
        atomic_write(path, "".join(dumps(row) + "\n" for row in existing + additions))


def write_human_groups(run_dir, key, groups):
    exam_id = key["exam_id"]
    ideas = load_ideas(run_dir, exam_id)
    mapped = [[key["items"][item]["idea_ids"][0] for item in group] for group in groups]
    chosen = [idea for group in mapped for idea in group]
    if any(idea not in ideas for idea in chosen):
        raise ValueError("A grouped idea no longer exists in the recording")
    root = Path(run_dir).resolve() / exam_id
    outputs = {root / "human_groups.json": dumps({"exam_id": exam_id, "groups": mapped}) + "\n",
               root / "human_group_ideas.jsonl": "".join(dumps(ideas[idea]) + "\n" for idea in sorted(chosen))}
    for path, text in outputs.items():
        if path.exists() and path.read_text(encoding="utf-8") != text:
            raise ValueError(f"{path.name} already exists with different content")
    for path, text in outputs.items():
        atomic_write(path, text)


def reveal(run_dir, sheet_id):
    """Merge lineage back only after every item is answered; conservation is verified a second time."""
    path = sheet_path(run_dir, sheet_id)
    sheet = load_public_sheet(run_dir, sheet_id)
    with recording_lock(path.parent), recording_lock(path):
        key = load_key(run_dir, sheet)
        check_conservation(key.get("view"), key["items"], key["counts"], key["reused"])
        check_issued(load_issued(path), key, [item["item_id"] for item in sheet["items"]])
        answers = load_answers(run_dir, sheet)
        done = progress(sheet, answers)
        if done["done"] != done["total"]:
            raise ValueError(f"Not finished: {done['done']} of {done['total']} answered")
        fingerprint = digest(answers)
        marker = path / "revealed.json"
        if marker.exists():
            previous = read_file(marker)
            if previous.get("answers_digest") != fingerprint:
                raise ValueError("Answers changed after this sheet was revealed")
            return previous["result"]
        if sheet["kind"] == "group":
            write_human_groups(run_dir, key, answers)
            result = {"sheet_id": sheet_id, "kind": "group", "groups": len(answers), "counts": key["counts"]}
        else:
            table = load_table(run_dir, key["exam_id"], load_ideas(run_dir, key["exam_id"]))
            rows = verdict_rows(table, key, answers)
            write_verdicts(run_dir, key, rows)
            result = {"sheet_id": sheet_id, "kind": "score", "verdicts": len(rows), "counts": key["counts"]}
            if key["view"] == "all":
                # Frozen counts cannot see later dedupe. Report it instead of refusing: one open sheet per exam
                # means a refusal here would block the exam for good.
                judged = {row["mechanism_id"] for row in rows} | set(key["reused"])
                result["unjudged_mechanisms"] = sorted({row["mechanism_id"] for row in table["mechanisms"]} - judged)
        atomic_write(marker, dumps({"answers_digest": fingerprint, "result": result}) + "\n")
        return result


def check_results(run_dir, sheet_id):
    """Check samples scored by mistake, and repeats compared on questions two and three only.

    Question one is polluted by the first viewing, so it never counts as an inconsistency.
    """
    sheet = load_public_sheet(run_dir, sheet_id)
    if sheet["kind"] != "score":
        raise ValueError("Group sheets have no check samples")
    key = load_key(run_dir, sheet)
    answers = load_answers(run_dir, sheet)
    if len(answers) != len(sheet["items"]):
        raise ValueError("Check results need a finished sheet")
    originals = {item["mechanism_id"]: answers[item_id] for item_id, item in key["items"].items() if item["type"] == "target"}
    bad = [answers[item_id] for item_id, item in key["items"].items() if item["type"] == "check_bad"]
    repeats = [(answers[item_id], originals[item["mechanism_id"]]) for item_id, item in key["items"].items()
               if item["type"] == "repeat"]
    inconsistent = sum(new["care"] != old["care"] or bool(new["fatal_reason"]) != bool(old["fatal_reason"])
                       for new, old in repeats)
    return {"check_bad": {"total": len(bad), "scored": sum(scored(row) for row in bad)},
            "repeat": {"total": len(repeats), "consistent": len(repeats) - inconsistent, "inconsistent": inconsistent}}


def run_reveal(args):
    print(dumps(reveal(args.run_dir, args.sheet_id)))


def run_sheet(args):
    if args.exam_dir is None:
        raise ValueError("Sheets require --exam-dir: the page shows the exam's question")
    if args.kind == "group":
        if args.all or args.configs or args.rewrite_config:
            raise ValueError("Group sheets take no --all, --configs or --rewrite-config")
    elif args.all and (args.count is not None or args.rewrite_config):
        raise ValueError("--all shows everything without rewriting; it takes no --count or --rewrite-config")
    if Path(args.run_dir).resolve().is_relative_to(Path(args.exam_dir).resolve()):
        raise ValueError("run-dir must not be inside the read-only exam-dir")
    matches = [row for row in load_exams(args.exam_dir) if row["id"] == args.exam_id]
    if not matches:
        raise ValueError("exam-id was not found")
    exam = matches[0]
    configs = [name for name in (args.configs or "").split(",") if name] or None
    options = {}
    if args.kind == "score" and not args.all:
        if args.rewrite_config is None:
            raise ValueError("Comparison sheets require --rewrite-config; rewriting is mandatory")
        config = load_config(args.rewrite_config)
        backend = stage_backend(config, fake_rewrite)
        model = args.model if args.model is not None else config["default_model"]
        if model not in config["models"]:
            raise ValueError("Rewrite model must be in configured models")
        options = {"backend": backend, "model": model,
                   "ledger": BudgetLedger(args.ledger or default_ledger_path(backend.name, args.run_dir),
                                          args.limit if args.limit is not None else config["budget_usd"]),
                   "max_output_tokens": args.max_output_tokens if args.max_output_tokens is not None else config["max_output_tokens"]}
    try:
        result = make_sheet(args.run_dir, exam, kind=args.kind, sheet_id=args.sheet_id, seed=args.seed, all_view=args.all,
                            configs=configs, count=args.count, per_unit=args.per_unit, check_samples=args.check_samples,
                            repeats=args.repeats, rewrite_batch=args.rewrite_batch, rewrite_attempt=args.rewrite_attempt,
                            **options)
    except NotStarted as exc:
        raise ValueError(f"Sheet stopped before provider startup: {exc}") from exc
    print(dumps(result))


def register(subparsers):
    parser = subparsers.add_parser("sheet", help="Issue a blind grouping or scoring sheet")
    parser.add_argument("--kind", choices=("score", "group"), required=True)
    parser.add_argument("--all", action="store_true", help="Full view: every mechanism, not rewritten")
    parser.add_argument("--configs", help="Comma-separated configs for a comparison sheet")
    parser.add_argument("--rewrite-config", type=Path)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--exam-dir", type=Path)
    parser.add_argument("--exam-id", required=True)
    parser.add_argument("--sheet-id", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--count", type=int, help=f"Default: {DEFAULT_COMPARE_COUNT} for comparison sheets, {DEFAULT_GROUP_COUNT} for group sheets")
    parser.add_argument("--per-unit", type=int)
    parser.add_argument("--check-samples", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--rewrite-batch", type=int, default=10)
    parser.add_argument("--rewrite-attempt", type=int, default=1,
                        help="Raise explicitly to pay for a new rewrite after a failure; never automatic")
    parser.add_argument("--model")
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--limit")
    parser.add_argument("--max-output-tokens", type=int)
    parser.set_defaults(func=lambda args: cli_boundary(run_sheet, args))
    parser = subparsers.add_parser("reveal", help="Merge lineage back after every item is answered")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--sheet-id", required=True)
    parser.set_defaults(func=lambda args: cli_boundary(run_reveal, args))
