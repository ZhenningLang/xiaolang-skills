"""Sheet selection, mandatory rewriting, conservation, verdict reuse, answers and reveal."""

import contextlib
from decimal import Decimal
import io
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evals.brainstorm import dedupe, sheet
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetExceeded, BudgetLedger, LedgerError
from evals.brainstorm.exam import load_exams
from evals.brainstorm.prompts import wrap_data
from evals.brainstorm.record import read_rows


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "review_run"
EXAM_ID = "review_sample"
A, B = "cfg_canary_a", "cfg_canary_b"
UNITS = {f"{A}/rep-1": {"m-001", "m-002", "m-004", "m-006", "m-008", "m-010"},
         f"{A}/rep-2": {"m-001", "m-004", "m-005", "m-007", "m-009", "m-011"},
         f"{B}/rep-1": {"m-001", "m-002", "m-006", "m-007", "m-010", "m-012"},
         f"{B}/rep-2": {"m-001", "m-005", "m-008", "m-009", "m-011", "m-012"}}
ANSWER = {"unthought": True, "care": True, "fatal_reason": None, "want_to_try": False}
LINEAGE = re.compile(r"cfg_canary|model_canary|method_canary|card_canary|i-[0-9a-f]{12}|m-\d{3}")
TAINT = ("来源 model_canary_x，采用method_canary_y，卡片card_canary_1的，配置 cfg_canary_a，编号m-001和i-0123456789ab。"
         "又如card_canary_1-方法、方法-method_canary_y、cfg_canary_a_配置、ｍ－００１、ｍｏｄｅｌ＿ｃａｎａｒｙ＿ｘ。")
TAINT_FULL_WIDTH = ("ｍ－００１", "ｍｏｄｅｌ＿ｃａｎａｒｙ＿ｘ")
# Every way a source value can sit in real text. Names and identifiers share one matrix and one boundary.
SOURCE_FORMS = ("编号{}。", "{}的做法", "来自{}的做法", "见 {} 。", "（{}）", "{}", "{}，然后", "「{}」",
                "ref-{}", "idea_{}", "{}-方法", "方法-{}", "{}_方法", "方法_{}", "{}-2", "ｘ{}ｙ")
IDENTIFIERS = ("m-001", "m-12345", "M-001", "i-0123456789ab", "I-0123456789AB")
SOURCE_NAMES = {"method": "method_canary_y", "model": "MODEL_CANARY_X", "card": "card_canary_1",
                "config": "cfg_canary_a", "level": "pressure4"}


def full_width(text):
    return "".join(chr(ord(char) + 0xfee0) if "!" <= char <= "~" else char for char in text)


def spellings(value):
    """The value itself, full width, and with the hyphens word processors substitute."""
    return value, full_width(value), value.replace("-", "\u2011"), value.replace("-", "\uff0d")


def fixture_exam():
    return load_exams(FIXTURE / "exams")[0]


def copy_run(root, name="run"):
    target = Path(root) / name
    shutil.copytree(FIXTURE / "run", target)
    return target


def key_of(run, sheet_id):
    return json.loads((run / "sheets" / sheet_id / "sheet.key.json").read_text(encoding="utf-8"))


def public_of(run, sheet_id):
    return json.loads((run / "sheets" / sheet_id / "sheet.json").read_text(encoding="utf-8"))


def table_of(run):
    return json.loads((run / EXAM_ID / "mechanisms.json").read_text(encoding="utf-8"))


def verdict_line(mechanism_id, idea_ids, **changes):
    return {"exam_id": EXAM_ID, "mechanism_id": mechanism_id, "label": "x", "idea_ids": idea_ids, **ANSWER,
            "sheet_id": "old", "ts": "2026-11-03T00:00:00+00:00", **changes}


def answer_all(run, sheet_id, **changes):
    items = public_of(run, sheet_id)["items"]
    sheet.merge_answers(run, sheet_id, {"sheet_id": sheet_id, "answers": [
        {"item_id": item["item_id"], **ANSWER, **changes} for item in items]})
    return items


class SheetCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.run_dir = copy_run(self.root)
        self.exam = fixture_exam()
        self.ledger = BudgetLedger(self.root / "ledger.jsonl", "50")

    def all_sheet(self, sheet_id="s_all", **options):
        return sheet.make_sheet(self.run_dir, self.exam, kind="score", sheet_id=sheet_id, seed=7, all_view=True, **options)

    def compare_sheet(self, sheet_id="s_cmp", *, respond=None, backend=None, run_dir=None, exam=None, seed=7, **options):
        options.setdefault("per_unit", 3)
        return sheet.make_sheet(run_dir or self.run_dir, exam or self.exam, kind="score", sheet_id=sheet_id, seed=seed,
                                configs=[A, B], backend=backend or FakeBackend(responses=respond or sheet.fake_rewrite),
                                ledger=self.ledger, model="rewrite_model", **options)

    def assertNoSheet(self, sheet_id, run_dir=None):
        self.assertFalse((Path(run_dir or self.run_dir) / "sheets" / sheet_id / "sheet.json").exists())


class CompareSelectionTests(SheetCase):
    def test_each_unit_gets_exactly_k_from_its_own_non_baseline_set(self):
        self.compare_sheet()
        key = key_of(self.run_dir, "s_cmp")
        self.assertEqual(set(key["units"]), set(UNITS))
        for name, rows in key["units"].items():
            self.assertEqual(len(rows), 3)
            self.assertLessEqual(set(rows), UNITS[name])

    def test_shared_order_is_a_common_priority_and_config_reps_keeps_every_producer(self):
        self.compare_sheet()
        key = key_of(self.run_dir, "s_cmp")
        priority = sorted(row["mechanism_id"] for row in table_of(self.run_dir)["mechanisms"])
        random.Random("7:priority").shuffle(priority)
        for name, expected in UNITS.items():
            self.assertEqual(key["units"][name], [m for m in priority if m in expected][:3])
        targets = [item for item in key["items"].values() if item["type"] == "target"]
        self.assertEqual(len({item["mechanism_id"] for item in targets}), len(targets))
        producers = {}
        ideas = sheet.load_ideas(self.run_dir, EXAM_ID)
        for row in table_of(self.run_dir)["mechanisms"]:
            producers[row["mechanism_id"]] = sorted({f"{ideas[i]['config']}/rep-{ideas[i]['rep']}" for i in row["idea_ids"]})
        for item in targets:
            self.assertEqual(item["config_reps"], producers[item["mechanism_id"]])

    def test_a_producer_outside_the_sample_keeps_its_attribution_but_not_a_scored_slot(self):
        self.compare_sheet(per_unit=3)
        key = key_of(self.run_dir, "s_cmp")
        missed = [(name, item) for item in key["items"].values() if item["type"] == "target"
                  for name in item["config_reps"] if item["mechanism_id"] not in key["units"].get(name, [])]
        self.assertTrue(missed, "the fixture must have a producer that the sample skipped")
        self.assertTrue(all(len(rows) == 3 for rows in key["units"].values()))

    def test_same_seed_repeats_mechanisms_and_order_and_other_seed_differs(self):
        def sequence(name, seed):
            run = copy_run(self.root, name)
            self.compare_sheet("s_seq", run_dir=run, seed=seed)
            items = key_of(run, "s_seq")["items"].values()
            return [(item["type"], item["mechanism_id"], item["original_text"]) for item in items]
        first = sequence("one", 7)
        self.assertEqual(first, sequence("two", 7))
        self.assertNotEqual(first, sequence("three", 8))

    def test_baseline_only_mechanisms_never_enter_a_comparison(self):
        self.compare_sheet(per_unit=6)
        key = key_of(self.run_dir, "s_cmp")
        selected = {m for rows in key["units"].values() for m in rows}
        self.assertNotIn("m-003", selected)
        # m-004 exists in this unit only at baseline level, so it does not belong to the unit.
        self.assertNotIn("m-004", key["units"][f"{B}/rep-1"])

    def test_automatic_k_is_the_largest_within_count_and_warns_below_ten(self):
        result = self.compare_sheet(per_unit=None, count=8)
        key = key_of(self.run_dir, "s_cmp")
        priority = sorted(row["mechanism_id"] for row in table_of(self.run_dir)["mechanisms"])
        random.Random("7:priority").shuffle(priority)
        def union(k):
            return {m for expected in UNITS.values() for m in [p for p in priority if p in expected][:k]}
        k = len(next(iter(key["units"].values())))
        self.assertLessEqual(len(union(k)), 8)
        self.assertGreater(len(union(k + 1)), 8)
        self.assertTrue(any("below the 10" in text for text in result["warnings"]))

    def test_automatic_k_counts_only_unreused_mechanisms_against_the_limit(self):
        table = {row["mechanism_id"]: row for row in table_of(self.run_dir)["mechanisms"]}
        rows = [verdict_line(m, table[m]["idea_ids"]) for m in ("m-001", "m-002", "m-005", "m-007")]
        (self.run_dir / EXAM_ID / "verdicts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        result = self.compare_sheet(per_unit=None, count=7)
        self.assertLessEqual(result["counts"]["presented_target"], 7)
        self.assertEqual(result["counts"]["presented_target"] + result["counts"]["reused"], result["counts"]["selected_target"])

    def test_explicit_per_unit_above_smallest_unit_is_an_error_not_a_silent_shrink(self):
        with self.assertRaisesRegex(ValueError, "exceeds the smallest unit"):
            self.compare_sheet(per_unit=7)
        self.assertNoSheet("s_cmp")

    def test_count_that_cannot_hold_one_per_unit_is_an_error(self):
        priority = sorted(row["mechanism_id"] for row in table_of(self.run_dir)["mechanisms"])
        seed = next(s for s in range(100) if len({[p for p in self._order(priority, s) if p in expected][0]
                                                  for expected in UNITS.values()}) > 1)
        with self.assertRaisesRegex(ValueError, "cannot hold one item"):
            self.compare_sheet(per_unit=None, count=1, seed=seed)
        self.assertNoSheet("s_cmp")

    @staticmethod
    def _order(priority, seed):
        rows = list(priority)
        random.Random(f"{seed}:priority").shuffle(rows)
        return rows

    def test_incomplete_or_missing_unit_is_rejected_before_any_call(self):
        summary = self.run_dir / EXAM_ID / A / "rep-1" / "run_summary.json"
        data = json.loads(summary.read_text())
        summary.write_text(json.dumps({**data, "complete": False}))
        backend = FakeBackend(responses=sheet.fake_rewrite)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.compare_sheet(backend=backend)
        self.assertEqual(backend.calls, 0)
        self.assertNoSheet("s_cmp")
        summary.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "no recorded units"):
            sheet.make_sheet(self.run_dir, self.exam, kind="score", sheet_id="s_cmp", seed=1, configs=["cfg_absent"],
                             backend=backend, ledger=self.ledger, model="m")


class RewriteTests(SheetCase):
    def prompts(self, **options):
        seen = []
        def respond(prompt, model, maximum):
            seen.append(prompt)
            return sheet.fake_rewrite(prompt, model, maximum)
        self.compare_sheet(respond=respond, **options)
        return seen

    def test_every_presented_item_passes_the_ledger_inside_a_data_boundary(self):
        prompts = self.prompts()
        key = key_of(self.run_dir, "s_cmp")
        sent = []
        for prompt in prompts:
            self.assertIn(wrap_data(self.exam["question"]), prompt)
            rows = json.loads("\n".join(prompt.rsplit("\nItems:\n", 1)[1].split("\n")[2:-1]))
            self.assertIn(wrap_data(rows), prompt)
            self.assertTrue(all(set(row) == {"item", "text"} and row["item"].startswith("r-") for row in rows))
            sent.extend(row["text"] for row in rows)
        self.assertEqual(sorted(sent), sorted(item["original_text"] for item in key["items"].values()))
        settled = [row for row in read_rows(self.ledger.path) if row["event"] == "settle"]
        self.assertEqual(len(settled), len(prompts))

    def test_prompts_hold_no_lineage_or_hidden_fields(self):
        for prompt in self.prompts():
            for secret in ("canary", "CANARY", "planted", "hidden_answers", "隐藏答案", "idea_id", "mechanism_id", "m-00"):
                self.assertNotIn(secret, prompt)

    def test_key_maps_original_to_rewritten_and_public_shows_only_rewritten(self):
        self.compare_sheet()
        key, public = key_of(self.run_dir, "s_cmp"), public_of(self.run_dir, "s_cmp")
        self.assertEqual(list(key["items"]), [item["item_id"] for item in public["items"]])
        for row in public["items"]:
            item = key["items"][row["item_id"]]
            self.assertEqual(item["rewritten_text"], "平实版：" + " ".join(item["original_text"].split()))
            self.assertEqual(row["text"], item["rewritten_text"])
            self.assertEqual(row["variants"], [])

    def test_bad_responses_and_failed_calls_never_issue_a_sheet(self):
        def missing(prompt, model, maximum):
            data = json.loads(sheet.fake_rewrite(prompt, model, maximum))
            return json.dumps({"rewrites": data["rewrites"][1:]})
        cases = [("bad_json", {"respond": lambda *a: "not json"}, ValueError, "invalid_response"),
                 ("missing", {"respond": missing}, ValueError, "invalid_response"),
                 ("paid", {"backend": FakeBackend(failure="paid_error")}, LedgerError, "PaidCallError"),
                 ("unknown", {"backend": FakeBackend(failure="unknown")}, LedgerError, "CallUncertain"),
                 ("unstarted", {"backend": FakeBackend(failure="not_started")}, RuntimeError, "NotStarted")]
        for name, options, error, reason in cases:
            with self.subTest(name):
                sheet_id = f"s_{name}"
                with self.assertRaises(error):
                    self.compare_sheet(sheet_id, **options)
                self.assertNoSheet(sheet_id)
                self.assertFalse((self.run_dir / "sheets" / sheet_id / "sheet.key.json").exists())
                failures = read_rows(self.run_dir / "sheets" / sheet_id / "failures.jsonl")
                self.assertEqual(failures[-1]["reason"], reason)

    def test_rewrite_output_structure_is_uniform_whatever_the_model_returns(self):
        def structured(prompt, model, maximum):
            data = json.loads(sheet.fake_rewrite(prompt, model, maximum))
            for row in data["rewrites"]:
                row["text"] = "一个标题\n\n" + row["text"] + "\n第二段"
            return json.dumps(data)
        self.compare_sheet(respond=structured)
        for row in public_of(self.run_dir, "s_cmp")["items"]:
            self.assertNotIn("\n", row["text"])

    def test_rewrite_prompt_asks_for_one_paragraph(self):
        self.assertIn("single paragraph", sheet.rewrite_prompt("q", [{"item": "r-0", "text": "t"}]))

    def test_comparison_refuses_to_skip_rewriting(self):
        with self.assertRaisesRegex(ValueError, "require a rewrite backend"):
            sheet.make_sheet(self.run_dir, self.exam, kind="score", sheet_id="s_cmp", seed=1, configs=[A, B], per_unit=2)
        self.assertNoSheet("s_cmp")


class BudgetAndResumeTests(SheetCase):
    def cli(self, *extra, run=None, ledger=None, sheet_id="s_budget"):
        config = self.root / "rewrite.json"
        config.write_text(json.dumps({"name": "rewriter", "default_model": "rewrite_model",
                                      "backend": {"kind": "fake", "usd": "0.01", "reserve_usd": "0.02"}}))
        return subprocess.run([sys.executable, "-m", "evals.brainstorm", "sheet", "--kind", "score", "--configs", f"{A},{B}",
                               "--rewrite-config", str(config), "--run-dir", str(run or self.run_dir), "--exam-dir", str(FIXTURE / "exams"),
                               "--exam-id", EXAM_ID, "--sheet-id", sheet_id, "--seed", "7", "--per-unit", "6",
                               "--ledger", str(ledger or self.ledger.path), *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)

    @staticmethod
    def spend(path):
        rows = read_rows(path)
        return sum(Decimal(str(row["usd"])) for row in rows if row["event"] == "settle"), rows

    def test_budget_stop_exits_three_and_resume_matches_a_single_run(self):
        stopped = self.cli("--limit", "0.025")
        self.assertEqual(stopped.returncode, 3, stopped.stderr)
        self.assertNoSheet("s_budget")
        partial, rows = self.spend(self.ledger.path)
        self.assertEqual(partial, Decimal("0.01"))
        self.assertEqual(sum(row["event"] == "settle" for row in rows), 1)
        resumed = self.cli("--limit", "1")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue((self.run_dir / "sheets/s_budget/sheet.json").exists())
        other_run, other_ledger = copy_run(self.root, "other"), self.root / "other.jsonl"
        once = self.cli("--limit", "1", run=other_run, ledger=other_ledger)
        self.assertEqual(once.returncode, 0, once.stderr)
        total, rows = self.spend(self.ledger.path)
        expected, expected_rows = self.spend(other_ledger)
        self.assertEqual(total, expected)
        self.assertEqual(sum(row["event"] == "settle" for row in rows), sum(row["event"] == "settle" for row in expected_rows))

    def test_failed_call_exit_code_is_nonzero_without_a_sheet(self):
        config = self.root / "bad.json"
        config.write_text(json.dumps({"name": "rewriter", "default_model": "rewrite_model",
                                      "backend": {"kind": "fake", "failure": "paid_error"}}))
        result = subprocess.run([sys.executable, "-m", "evals.brainstorm", "sheet", "--kind", "score", "--configs", f"{A},{B}",
                                 "--rewrite-config", str(config), "--run-dir", str(self.run_dir), "--exam-dir", str(FIXTURE / "exams"),
                                 "--exam-id", EXAM_ID, "--sheet-id", "s_bad", "--seed", "7", "--per-unit", "2",
                                 "--ledger", str(self.ledger.path)], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertNoSheet("s_bad")

    def test_rerun_replays_the_cached_rejection_and_only_a_new_attempt_pays(self):
        def tainted(prompt, model, maximum):
            data = json.loads(sheet.fake_rewrite(prompt, model, maximum))
            data["rewrites"][0]["text"] += " method_canary_y"
            return json.dumps(data)
        backend = FakeBackend(responses=tainted)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "source term"):
                self.compare_sheet(backend=backend)
            self.assertEqual(backend.calls, 1)
        before = len(read_rows(self.ledger.path))
        clean = FakeBackend(responses=sheet.fake_rewrite)
        self.compare_sheet(backend=clean, rewrite_attempt=2, per_unit=3)
        self.assertGreater(clean.calls, 0)
        self.assertGreater(len(read_rows(self.ledger.path)), before)
        self.assertEqual(read_rows(self.run_dir / "sheets/s_cmp/failures.jsonl"), [])
        self.assertTrue(all(row["attempt"] in (1, 2) for row in read_rows(self.run_dir / "sheets/s_cmp/calls.jsonl")))

    def test_key_written_without_sheet_resumes_and_conflicting_inputs_are_refused(self):
        self.all_sheet()
        key = key_of(self.run_dir, "s_all")
        (self.run_dir / "sheets/s_all/sheet.json").unlink()
        self.all_sheet()
        self.assertEqual(list(key_of(self.run_dir, "s_all")["items"]), list(key["items"]))
        (self.run_dir / "sheets/s_all/sheet.json").unlink()
        with self.assertRaisesRegex(ValueError, "different inputs"):
            self.all_sheet(repeats=0)

    def test_issuing_the_same_sheet_twice_is_refused(self):
        self.all_sheet()
        with self.assertRaisesRegex(ValueError, "already issued"):
            self.all_sheet()

    def test_budget_exception_type_reaches_callers(self):
        small = BudgetLedger(self.root / "small.jsonl", "0.01")
        with self.assertRaises(BudgetExceeded):
            sheet.make_sheet(self.run_dir, self.exam, kind="score", sheet_id="s_small", seed=7, configs=[A, B], per_unit=2,
                             backend=FakeBackend(responses=sheet.fake_rewrite), ledger=small, model="m")
        self.assertNoSheet("s_small")


class RewriteScanTests(SheetCase):
    def taint(self, term):
        def respond(prompt, model, maximum):
            data = json.loads(sheet.fake_rewrite(prompt, model, maximum))
            data["rewrites"][-1]["text"] += f" 来自 {term}。"
            return json.dumps(data)
        return respond

    def test_each_lineage_kind_refuses_the_sheet_without_echoing_the_rewrite(self):
        for number, (term, kind) in enumerate((("method_canary_y", "method"), ("MODEL_CANARY_X", "model"),
                                               ("card_canary_1", "card"), ("cfg_canary_b", "config"),
                                               ("m-001", "identifier"), ("i-0123456789ab", "identifier"),
                                               ("pressure4", "level"), ("round2", "level"))):
            with self.subTest(term):
                sheet_id = f"s_scan{number}"
                with self.assertRaises(ValueError) as caught:
                    self.compare_sheet(sheet_id, respond=self.taint(term))
                self.assertIn(kind, str(caught.exception))
                self.assertNotIn("平实版", str(caught.exception))
                self.assertNotIn(term, str(caught.exception))
                self.assertNoSheet(sheet_id)
                failures = read_rows(self.run_dir / "sheets" / sheet_id / "failures.jsonl")
                self.assertEqual(failures[-1]["reason"], "lineage_term")
                self.assertNotIn("平实版", json.dumps(failures, ensure_ascii=False))

    def scan_patterns(self):
        return sheet.term_patterns(sheet.lineage_terms(sheet.load_ideas(self.run_dir, EXAM_ID), self.exam, ()))

    def test_every_source_form_and_spelling_is_found_and_masked(self):
        patterns = self.scan_patterns()
        cases = [("identifier", value) for value in IDENTIFIERS] + [(kind, name) for kind, name in SOURCE_NAMES.items()]
        for kind, value in cases:
            for spelled in spellings(value):
                for form in SOURCE_FORMS:
                    if form == "ｘ{}ｙ" and spelled == full_width(value):
                        continue  # Full-width letters around a full-width value simply form a longer word.
                    text = form.format(spelled)
                    with self.subTest(text):
                        self.assertEqual(sheet.lineage_hits(text, patterns), [kind])
                        self.assertNotIn(spelled, sheet.mask_wordings(text, [], patterns)[0])

    def test_masking_cuts_only_the_match_and_keeps_full_width_punctuation(self):
        patterns = self.scan_patterns()
        text = "用ｃａｒｄ＿ｃａｎａｒｙ＿１，然后（继续）：Ａ４纸。"
        self.assertEqual(sheet.mask_wordings(text, [], patterns)[0], "用[已隐去]，然后（继续）：Ａ４纸。")

    def test_near_values_are_not_flagged(self):
        patterns = sheet.term_patterns({"card": {"c1"}, "config": set(), "model": set(), "method": set()})
        for text in ("team-001", "item-001", "hi-0123456789ab", "m-01", "i-0123456789abc", "i-0123456789ag",
                     "c10", "xc1", "ｃ１０", "ｘｃ１", "ｍ－０１"):
            with self.subTest(text):
                self.assertEqual(sheet.lineage_hits(text, patterns), [])

    def test_separators_inside_a_name_are_interchangeable(self):
        patterns = self.scan_patterns()
        for text in ("Pressure 3 下的点子", "pressure3", "来自pressure 1的", "round 2 产出", "Round2", "Pressure  3",
                     "Round\u30001", "pressure-3", "round_1"):
            with self.subTest(text):
                self.assertEqual(sheet.lineage_hits(text, patterns), ["level"])
        for text in ("method canary y", "method-canary-y", "METHOD CANARY_Y"):
            with self.subTest(text):
                self.assertEqual(sheet.lineage_hits(text, patterns), ["method"])

    def test_internal_field_names_in_text_are_found(self):
        patterns = self.scan_patterns()
        for text in ('{"idea_id": "x"}', "mechanism_id", "config_reps 字段"):
            with self.subTest(text):
                self.assertEqual(sheet.lineage_hits(text, patterns), ["field"])
        # An ordinary word, so only the response shape guards it: see the known limits in the handoff.
        self.assertEqual(sheet.lineage_hits("a representative sample", patterns), [])

    def test_blank_source_names_match_nothing(self):
        patterns = sheet.term_patterns({"method": {"", "  ", "-"}, "config": set(), "model": set(), "card": set()})
        self.assertEqual(patterns, [("identifier", sheet.IDENTIFIER)])
        self.assertEqual(sheet.mask_wordings("正文", [], patterns)[0], "正文")

    def test_longer_names_are_masked_before_the_names_they_contain(self):
        patterns = sheet.term_patterns({"config": {"gpt-5"}, "model": {"openai/gpt-5"}, "method": set(), "card": set()})
        self.assertEqual(sheet.mask_wordings("用 openai/gpt-5 生成", [], patterns)[0], "用 [已隐去] 生成")

    def test_identifier_next_to_chinese_refuses_a_comparison_sheet(self):
        for number, text in enumerate(("来自m-001的做法", "编号i-0123456789ab。", "编号ｍ－００１。", "编号i\u20110123456789ab的",
                                       "用method_canary_y-方法的做法", "方法-method_canary_y的做法", "来自cfg_canary_b_配置",
                                       "用ｃａｒｄ＿ｃａｎａｒｙ＿１的", "pressure-3 下", "Pressure  3 下")):
            sheet_id = f"s_adjacent{number}"
            with self.subTest(text), self.assertRaisesRegex(ValueError, "source term"):
                self.compare_sheet(sheet_id, respond=self.taint(text))
            self.assertNoSheet(sheet_id)

    def test_word_boundaries_do_not_flag_a_longer_identifier(self):
        patterns = sheet.term_patterns({"card": {"c1"}, "config": set(), "model": set(), "method": set()})
        self.assertEqual(sheet.lineage_hits("see c10 and xc1", patterns), [])
        # Only letters and digits extend a word. A separator never shields a name, so c1_b is cut too:
        # for a blind review a false cut is the safer mistake.
        self.assertEqual(sheet.lineage_hits("c1_b", patterns), ["card"])
        self.assertEqual(sheet.lineage_hits("用 C1。", patterns), ["card"])

    def test_injection_marker_only_refuses_when_it_newly_appears(self):
        with self.assertRaises(ValueError) as caught:
            self.compare_sheet("s_marker", respond=self.taint("CANARY_INJECTION_MARK"))
        self.assertIn("injection marker", str(caught.exception))
        self.assertNoSheet("s_marker")
        self.assertEqual(read_rows(self.run_dir / "sheets/s_marker/failures.jsonl")[-1]["reason"], "injection_marker")
        # A marker already present in the source text is not something the rewrite introduced.
        carried = {**self.exam, "planted": [{"card_id": "card_canary_2", "kind": "injection", "marker": "。"}]}
        self.compare_sheet("s_carried", exam=carried, check_samples=0)
        self.assertTrue((self.run_dir / "sheets/s_carried/sheet.json").exists())


class FullViewTests(SheetCase):
    def test_full_view_calls_no_backend_and_writes_no_ledger(self):
        self.all_sheet()
        self.assertFalse(self.ledger.path.exists() and read_rows(self.ledger.path))
        self.assertFalse((self.run_dir / "sheets/s_all/calls.jsonl").exists())
        self.assertFalse((self.run_dir / "sheets/s_all/rewrite_state.json").exists())

    def test_variants_are_every_other_wording_and_exclude_the_shown_one(self):
        self.all_sheet()
        key, public = key_of(self.run_dir, "s_all"), public_of(self.run_dir, "s_all")
        ideas = sheet.load_ideas(self.run_dir, EXAM_ID)
        for row in public["items"]:
            item = key["items"][row["item_id"]]
            self.assertEqual(row["text"], item["original_text"])
            self.assertIsNone(item["rewritten_text"])
            expected = {sheet.idea_text(ideas[i]) for i in item["idea_ids"]} - {row["text"]}
            self.assertEqual(set(row["variants"]), expected)
            self.assertEqual(len(row["variants"]), len(expected))

    def test_variants_are_shuffled_by_seed(self):
        orders = set()
        for seed in range(6):
            run = copy_run(self.root, f"seed{seed}")
            sheet.make_sheet(run, self.exam, kind="score", sheet_id="s", seed=seed, all_view=True, repeats=0)
            key, public = key_of(run, "s"), public_of(run, "s")
            orders.update(tuple(row["variants"]) for row in public["items"]
                          if key["items"][row["item_id"]]["mechanism_id"] == "m-001"
                          and len(row["variants"]) == 3 and set(row["variants"]) == set(sorted(row["variants"])))
        self.assertGreater(len(orders), 1)

    def test_full_view_config_reps_lists_every_producing_unit_including_baseline(self):
        self.all_sheet(repeats=0)
        key = key_of(self.run_dir, "s_all")
        items = {item["mechanism_id"]: item for item in key["items"].values() if item["type"] == "target"}
        self.assertEqual(items["m-001"]["config_reps"], sorted(UNITS))
        self.assertEqual(items["m-003"]["config_reps"], [f"{A}/rep-1", f"{B}/rep-2"])
        self.assertEqual(items["m-004"]["config_reps"], [f"{A}/rep-1", f"{A}/rep-2", f"{B}/rep-1"])

    def taint_recordings(self):
        for path in (self.run_dir / EXAM_ID).glob("*/rep-*/ideas.jsonl"):
            rows = read_rows(path)
            path.write_text("".join(json.dumps({**row, "text": row["text"] + TAINT}, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    def test_lineage_in_original_text_warns_and_is_masked_everywhere_the_page_can_read(self):
        self.taint_recordings()
        result = self.all_sheet()
        self.assertTrue(any("source term" in text for text in result["warnings"]))
        public = public_of(self.run_dir, "s_all")
        self.assertIsNone(LINEAGE.search(json.dumps(public, ensure_ascii=False)))
        for spelled in TAINT_FULL_WIDTH:
            self.assertNotIn(spelled, json.dumps(public, ensure_ascii=False))
        self.assertEqual(result["counts"]["presented_target"], 15)
        self.assertIn("[已隐去]", public["items"][0]["text"])
        key = key_of(self.run_dir, "s_all")
        targets = [item for item in key["items"].values() if item["type"] == "target"]
        self.assertTrue(all("model_canary_x" in item["original_text"] for item in targets), "the key keeps the source")
        self.assertTrue(all(item["rewritten_text"] is None for item in targets), "masking is not rewriting")

    def test_warning_names_the_kinds_so_an_ordinary_word_used_as_a_name_is_noticed(self):
        path = self.run_dir / EXAM_ID / A / "rep-1" / "ideas.jsonl"
        rows = read_rows(path)
        path.write_text("".join(json.dumps({**row, "text": row["text"] + "用method_canary_y。"}, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        warning = next(text for text in self.all_sheet()["warnings"] if "source term" in text)
        self.assertRegex(warning, r"method: \d+")
        self.assertIn("Ordinary words", warning)

    def test_group_sheet_masks_lineage_too(self):
        self.taint_recordings()
        result = sheet.make_sheet(self.run_dir, {"id": EXAM_ID}, kind="group", sheet_id="s_grp", seed=3, count=8)
        self.assertTrue(any("source term" in text for text in result["warnings"]))
        text = json.dumps(public_of(self.run_dir, "s_grp"), ensure_ascii=False)
        self.assertIsNone(LINEAGE.search(text))
        for spelled in TAINT_FULL_WIDTH:
            self.assertNotIn(spelled, text)

    def test_masking_cannot_leave_a_duplicate_of_the_shown_wording(self):
        patterns = sheet.term_patterns({"model": {"model_x"}, "config": set(), "method": set(), "card": set()})
        shown, variants = sheet.mask_wordings("用 model_x", ["用 [已隐去]", "别的", "别的"], patterns)
        self.assertEqual((shown, variants), ("用 [已隐去]", ["别的"]))

    def test_incomplete_unit_warns_in_full_view_and_keeps_every_recorded_mechanism(self):
        summary = self.run_dir / EXAM_ID / A / "rep-1" / "run_summary.json"
        summary.write_text(json.dumps({**json.loads(summary.read_text()), "complete": False}))
        result = self.all_sheet()
        self.assertTrue(any("incomplete" in text for text in result["warnings"]))
        self.assertEqual(result["counts"]["presented_target"], 15)


class ConservationTests(SheetCase):
    def write_verdicts(self, *mechanisms):
        table = {row["mechanism_id"]: row for row in table_of(self.run_dir)["mechanisms"]}
        (self.run_dir / EXAM_ID / "verdicts.jsonl").write_text(
            "".join(json.dumps(verdict_line(m, table[m]["idea_ids"])) + "\n" for m in mechanisms))

    def test_first_round_counts(self):
        counts = self.all_sheet()["counts"]
        self.assertEqual(counts, {"mechanisms_total": 15, "selected_target": 15, "presented_target": 15,
                                  "reused": 0, "check_bad": 3, "repeat": 2})

    def test_reuse_counts(self):
        self.write_verdicts("m-001", "m-003")
        counts = self.all_sheet()["counts"]
        self.assertEqual((counts["presented_target"], counts["reused"], counts["selected_target"]), (13, 2, 15))

    def test_check_samples_and_repeats_stay_outside_the_equation(self):
        counts = self.all_sheet(check_samples=1, repeats=1)["counts"]
        self.assertEqual((counts["presented_target"], counts["check_bad"], counts["repeat"]), (15, 1, 1))
        self.assertEqual(len(key_of(self.run_dir, "s_all")["items"]), 17)

    def test_mechanism_shared_by_every_unit_counts_once(self):
        self.all_sheet(repeats=0)
        key = key_of(self.run_dir, "s_all")
        hits = [item for item in key["items"].values() if item["mechanism_id"] == "m-001"]
        self.assertEqual(len(hits), 1)
        self.assertTrue(all("m-001" in key["units"][name] for name in UNITS))
        self.assertEqual(key["counts"]["selected_target"], 15)

    def test_comparison_counts_hold_with_reuse(self):
        self.write_verdicts("m-001")
        counts = self.compare_sheet(per_unit=6)["counts"]
        self.assertEqual((counts["selected_target"], counts["presented_target"], counts["reused"]), (11, 10, 1))
        self.assertEqual(key_of(self.run_dir, "s_cmp")["reused"], ["m-001"])
        self.assertTrue(all("m-001" in rows for rows in key_of(self.run_dir, "s_cmp")["units"].values()))

    def test_dropping_a_row_refuses_to_issue_either_view(self):
        def drop(selected, verdicts):
            return [key for key in selected if key not in verdicts][1:]
        with patch.object(sheet, "presentable", drop):
            with self.assertRaisesRegex(ValueError, "Conservation failed"):
                self.all_sheet()
            with self.assertRaisesRegex(ValueError, "Conservation failed"):
                self.compare_sheet()
        self.assertNoSheet("s_all")
        self.assertNoSheet("s_cmp")
        self.assertFalse((self.run_dir / "sheets/s_all/sheet.key.json").exists())

    def test_tampered_key_blocks_reveal_and_writes_no_verdict(self):
        self.all_sheet()
        answer_all(self.run_dir, "s_all")
        path = self.run_dir / "sheets/s_all/sheet.key.json"
        original = path.read_text(encoding="utf-8")
        data = json.loads(original)
        data["counts"]["presented_target"] -= 1
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "Conservation failed"):
            sheet.reveal(self.run_dir, "s_all")
        data = json.loads(original)
        data["items"].pop(next(iter(data["items"])))
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "does not match"):
            sheet.reveal(self.run_dir, "s_all")
        self.assertFalse((self.run_dir / EXAM_ID / "verdicts.jsonl").exists())
        self.assertFalse((self.run_dir / "sheets/s_all/revealed.json").exists())


class VerdictReuseTests(SheetCase):
    def test_reused_mechanism_is_not_presented_and_stays_in_units(self):
        table = {row["mechanism_id"]: row for row in table_of(self.run_dir)["mechanisms"]}
        (self.run_dir / EXAM_ID / "verdicts.jsonl").write_text(json.dumps(verdict_line("m-001", table["m-001"]["idea_ids"])) + "\n")
        self.all_sheet()
        key = key_of(self.run_dir, "s_all")
        self.assertNotIn("m-001", [item["mechanism_id"] for item in key["items"].values()])
        self.assertEqual(key["reused"], ["m-001"])

    def test_new_wording_added_to_a_judged_mechanism_is_still_reused(self):
        table = table_of(self.run_dir)
        row = next(r for r in table["mechanisms"] if r["mechanism_id"] == "m-002")
        judged = list(row["idea_ids"])
        path = self.run_dir / EXAM_ID / B / "rep-2" / "ideas.jsonl"
        rows = read_rows(path)
        new = {**rows[0], "idea_id": "i-aaaaaaaaaaaa", "title": "新说法", "text": "另一种说法。", "level": "round1", "batch": 2}
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows + [new]), encoding="utf-8")
        row["idea_ids"].append(new["idea_id"])
        (self.run_dir / EXAM_ID / "mechanisms.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        (self.run_dir / EXAM_ID / "verdicts.jsonl").write_text(json.dumps(verdict_line("m-002", judged)) + "\n")
        self.all_sheet()
        self.assertIn("m-002", key_of(self.run_dir, "s_all")["reused"])

    def test_invalid_verdicts_are_errors_not_silent_reuse(self):
        table = {row["mechanism_id"]: row for row in table_of(self.run_dir)["mechanisms"]}
        cases = {"subset": verdict_line("m-001", table["m-002"]["idea_ids"]),
                 "unknown": verdict_line("m-099", table["m-001"]["idea_ids"]),
                 "field": {**verdict_line("m-001", table["m-001"]["idea_ids"]), "extra": 1},
                 "truthy": verdict_line("m-001", table["m-001"]["idea_ids"], care=1),
                 "nested": verdict_line("m-001", [table["m-001"]["idea_ids"]]),
                 "empty": verdict_line("m-001", []),
                 "mechanism_shape": verdict_line(["m-001"], table["m-001"]["idea_ids"])}
        for name, row in cases.items():
            with self.subTest(name):
                (self.run_dir / EXAM_ID / "verdicts.jsonl").write_text(json.dumps(row) + "\n")
                with self.assertRaises(ValueError):
                    self.all_sheet(f"s_{name}")
                self.assertNoSheet(f"s_{name}")

    def test_replay_partitions_do_not_stand_in_for_the_authoritative_table(self):
        table = self.run_dir / EXAM_ID / "mechanisms.json"
        replay = self.run_dir / "replay" / "trial" / EXAM_ID
        replay.mkdir(parents=True)
        shutil.copy(table, replay / "mechanisms.json")
        table.unlink()
        with self.assertRaisesRegex(ValueError, "Authoritative mechanisms.json"):
            self.all_sheet()

    def test_second_score_sheet_is_refused_until_the_first_is_revealed(self):
        self.all_sheet("s_one")
        with self.assertRaisesRegex(ValueError, "has not been revealed"):
            self.all_sheet("s_two")
        self.assertNoSheet("s_two")
        answer_all(self.run_dir, "s_one")
        sheet.reveal(self.run_dir, "s_one")
        result = self.all_sheet("s_two")
        self.assertEqual(result["counts"]["presented_target"], 0)
        self.assertEqual(result["counts"]["reused"], 15)
        self.assertEqual(public_of(self.run_dir, "s_two")["items"], [])
        self.assertEqual(sheet.reveal(self.run_dir, "s_two")["verdicts"], 0)


class CheckSampleTests(SheetCase):
    def test_check_items_are_indistinguishable_from_targets(self):
        for maker, name in ((self.all_sheet, "s_all"), (self.compare_sheet, "s_cmp")):
            maker()
            answer_all(self.run_dir, name)
            sheet.reveal(self.run_dir, name)
            key, public = key_of(self.run_dir, name), public_of(self.run_dir, name)
            types = {key["items"][row["item_id"]]["type"] for row in public["items"]}
            self.assertEqual(types, {"target", "check_bad", "repeat"})
            self.assertEqual({tuple(sorted(row)) for row in public["items"]}, {("item_id", "text", "variants")})
            # Same fields are not enough: the visible structure must not separate the two kinds.
            structure = {}
            for row in public["items"]:
                kind = "check" if key["items"][row["item_id"]]["type"] == "check_bad" else "real"
                structure.setdefault(kind, set()).add("\n" in row["text"])
            self.assertLessEqual(structure["check"], structure["real"], name)
            (self.run_dir / EXAM_ID / "verdicts.jsonl").unlink()

    def test_comparison_items_have_one_structure(self):
        self.compare_sheet()
        self.assertFalse(any("\n" in row["text"] for row in public_of(self.run_dir, "s_cmp")["items"]))

    def test_full_view_mixes_samples_only_when_they_cannot_be_told_apart(self):
        self.all_sheet()
        key, public = key_of(self.run_dir, "s_all"), public_of(self.run_dir, "s_all")
        real_lone = [row for row in public["items"] if not row["variants"] and key["items"][row["item_id"]]["type"] == "target"]
        checks = [row for row in public["items"] if key["items"][row["item_id"]]["type"] == "check_bad"]
        self.assertEqual(len(checks), 3)
        self.assertGreaterEqual(len(real_lone), len(checks))
        self.assertTrue(all("\n" in row["text"] for row in public["items"]))

    def test_full_view_skips_samples_without_a_title_line_and_says_so(self):
        exam = {**self.exam, "check_samples": ["只有一句话的样本。", "标题\n有正文的样本。"]}
        result = sheet.make_sheet(self.run_dir, exam, kind="score", sheet_id="s", seed=7, all_view=True)
        self.assertEqual(result["counts"]["check_bad"], 1)
        self.assertTrue(any("title line" in text for text in result["warnings"]))
        rows = [item for item in key_of(self.run_dir, "s")["items"].values() if item["type"] == "check_bad"]
        self.assertEqual(rows[0]["original_text"], "标题\n有正文的样本。")

    def test_comparison_applies_the_same_title_line_rule(self):
        # A kept title leaves a space after flattening, so a one-line sample would stand out there too.
        exam = {**self.exam, "check_samples": ["只有一句话的样本。", "标题\n有正文的样本。"]}
        result = self.compare_sheet(exam=exam)
        self.assertEqual(result["counts"]["check_bad"], 1)
        self.assertTrue(any("title line" in text for text in result["warnings"]))
        key, public = key_of(self.run_dir, "s_cmp"), public_of(self.run_dir, "s_cmp")
        spaced = {key["items"][row["item_id"]]["type"]: " " in row["text"] for row in public["items"]}
        self.assertTrue(spaced["check_bad"] and spaced["target"])

    def test_full_view_limits_samples_to_the_real_items_without_variants(self):
        table = {row["mechanism_id"]: row for row in table_of(self.run_dir)["mechanisms"]}
        (self.run_dir / EXAM_ID / "verdicts.jsonl").write_text("".join(
            json.dumps(verdict_line(m, table[m]["idea_ids"])) + "\n" for m in ("m-013", "m-014")))
        result = self.all_sheet()
        self.assertEqual(result["counts"]["check_bad"], 1)
        self.assertTrue(any("without other wordings" in text for text in result["warnings"]))

    def test_target_is_always_shown_before_its_repeat(self):
        for seed in range(30):
            run = copy_run(self.root, f"first{seed}")
            sheet.make_sheet(run, self.exam, kind="score", sheet_id="s", seed=seed, all_view=True, repeats=3)
            rows = list(key_of(run, "s")["items"].values())
            first = {}
            for position, row in enumerate(rows):
                if row["mechanism_id"]:
                    first.setdefault(row["mechanism_id"], row["type"])
            self.assertEqual(set(first.values()), {"target"}, seed)

    def test_samples_come_from_the_exam_and_absence_is_reported(self):
        self.all_sheet()
        key = key_of(self.run_dir, "s_all")
        bad = [item for item in key["items"].values() if item["type"] == "check_bad"]
        self.assertEqual({item["original_text"] for item in bad}, set(self.exam["check_samples"]))
        self.assertTrue(all(item["mechanism_id"] is None and item["idea_ids"] == [] and item["config_reps"] == [] for item in bad))
        exam = {key: value for key, value in self.exam.items() if key != "check_samples"}
        run = copy_run(self.root, "bare")
        result = sheet.make_sheet(run, exam, kind="score", sheet_id="s", seed=7, all_view=True)
        self.assertEqual(result["counts"]["check_bad"], 0)
        self.assertTrue(any("no check_samples" in text for text in result["warnings"]))

    def test_samples_share_rewrite_batches_with_targets_and_repeats_go_alone(self):
        prompts = []
        def respond(prompt, model, maximum):
            prompts.append(json.loads("\n".join(prompt.rsplit("\nItems:\n", 1)[1].split("\n")[2:-1])))
            return sheet.fake_rewrite(prompt, model, maximum)
        self.compare_sheet(respond=respond, per_unit=6, rewrite_batch=100)
        self.assertEqual(len(prompts), 2)
        mixed = {row["text"] for row in prompts[0]}
        self.assertTrue(set(self.exam["check_samples"]) <= mixed)
        self.assertEqual(len(prompts[1]), 2)
        self.assertFalse(set(self.exam["check_samples"]) & {row["text"] for row in prompts[1]})

    def test_repeats_never_sit_next_to_their_original_and_are_capped(self):
        for seed in range(20):
            run = copy_run(self.root, f"order{seed}")
            result = sheet.make_sheet(run, self.exam, kind="score", sheet_id="s", seed=seed, all_view=True, repeats=9)
            self.assertEqual(result["counts"]["repeat"], 3)
            self.assertTrue(any("Repeats reduced" in text for text in result["warnings"]))
            rows = list(key_of(run, "s")["items"].values())
            for left, right in zip(rows, rows[1:]):
                if "repeat" in (left["type"], right["type"]) and left["mechanism_id"]:
                    self.assertNotEqual(left["mechanism_id"], right["mechanism_id"])

    def test_full_view_repeat_prefers_another_wording(self):
        self.all_sheet(repeats=3)
        key = key_of(self.run_dir, "s_all")
        rows = list(key["items"].values())
        for repeat in (row for row in rows if row["type"] == "repeat"):
            original = next(row for row in rows if row["type"] == "target" and row["mechanism_id"] == repeat["mechanism_id"])
            self.assertNotEqual(repeat["original_text"], original["original_text"])

    def test_check_results_compare_only_questions_two_and_three(self):
        self.all_sheet()
        key = key_of(self.run_dir, "s_all")
        items = public_of(self.run_dir, "s_all")["items"]
        rows = {row["item_id"]: {**ANSWER} for row in items}
        target_of = {item["mechanism_id"]: item_id for item_id, item in key["items"].items() if item["type"] == "target"}
        repeats = [(item_id, item) for item_id, item in key["items"].items() if item["type"] == "repeat"]
        first, second = repeats
        rows[first[0]]["unthought"] = not rows[target_of[first[1]["mechanism_id"]]]["unthought"]
        rows[second[0]]["care"] = False
        for item_id, item in key["items"].items():
            if item["type"] == "check_bad":
                rows[item_id] = {**ANSWER}
                break
        sheet.merge_answers(self.run_dir, "s_all", {"sheet_id": "s_all", "answers": [{"item_id": k, **v} for k, v in rows.items()]})
        result = sheet.check_results(self.run_dir, "s_all")
        self.assertEqual(result["repeat"], {"total": 2, "consistent": 1, "inconsistent": 1})
        self.assertEqual(result["check_bad"], {"total": 3, "scored": 3})
        rows[second[0]] = {**rows[target_of[second[1]["mechanism_id"]]]}
        rows[target_of[second[1]["mechanism_id"]]] = {**ANSWER, "fatal_reason": "不成"}
        sheet.merge_answers(self.run_dir, "s_all", {"sheet_id": "s_all", "answers": [{"item_id": k, **v} for k, v in rows.items()]})
        self.assertEqual(sheet.check_results(self.run_dir, "s_all")["repeat"]["inconsistent"], 1)

    def test_scored_requires_all_three_conditions_and_ignores_want_to_try(self):
        self.assertTrue(sheet.scored(ANSWER))
        self.assertTrue(sheet.scored({**ANSWER, "want_to_try": True}))
        self.assertFalse(sheet.scored({**ANSWER, "unthought": False}))
        self.assertFalse(sheet.scored({**ANSWER, "care": False}))
        self.assertFalse(sheet.scored({**ANSWER, "fatal_reason": "不成"}))


class RevealTests(SheetCase):
    def test_unfinished_sheet_refuses_and_writes_nothing(self):
        self.all_sheet()
        first = public_of(self.run_dir, "s_all")["items"][0]
        sheet.merge_answers(self.run_dir, "s_all", {"sheet_id": "s_all", "answers": [{"item_id": first["item_id"], **ANSWER}]})
        with self.assertRaisesRegex(ValueError, "Not finished: 1 of 20"):
            sheet.reveal(self.run_dir, "s_all")
        self.assertFalse((self.run_dir / EXAM_ID / "verdicts.jsonl").exists())
        self.assertFalse((self.run_dir / "sheets/s_all/revealed.json").exists())

    def test_verdicts_hold_only_targets_with_contract_fields_and_table_values(self):
        self.all_sheet()
        answer_all(self.run_dir, "s_all", fatal_reason="说得出", want_to_try=True)
        result = sheet.reveal(self.run_dir, "s_all")
        self.assertEqual(result["verdicts"], 15)
        rows = read_rows(self.run_dir / EXAM_ID / "verdicts.jsonl")
        table = {row["mechanism_id"]: row for row in table_of(self.run_dir)["mechanisms"]}
        self.assertEqual({row["mechanism_id"] for row in rows}, set(table))
        for row in rows:
            self.assertEqual(set(row), sheet.VERDICT_FIELDS)
            self.assertEqual(row["label"], table[row["mechanism_id"]]["label"])
            self.assertEqual(row["idea_ids"], table[row["mechanism_id"]]["idea_ids"])
            self.assertEqual((row["fatal_reason"], row["want_to_try"], row["sheet_id"]), ("说得出", True, "s_all"))

    def test_reveal_twice_adds_nothing_and_changed_answers_are_refused(self):
        self.all_sheet()
        answer_all(self.run_dir, "s_all")
        first = sheet.reveal(self.run_dir, "s_all")
        path = self.run_dir / EXAM_ID / "verdicts.jsonl"
        before = path.read_bytes()
        self.assertEqual(sheet.reveal(self.run_dir, "s_all"), first)
        self.assertEqual(path.read_bytes(), before)
        answers = self.run_dir / "sheets/s_all/answers.json"
        data = json.loads(answers.read_text(encoding="utf-8"))
        next(iter(data["answers"].values()))["care"] = False
        answers.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed after"):
            sheet.reveal(self.run_dir, "s_all")
        self.assertEqual(path.read_bytes(), before)

    def test_answers_are_closed_after_reveal(self):
        self.all_sheet()
        items = answer_all(self.run_dir, "s_all")
        sheet.reveal(self.run_dir, "s_all")
        with self.assertRaises(sheet.SheetClosed):
            sheet.merge_answers(self.run_dir, "s_all", {"sheet_id": "s_all", "answers": [{"item_id": items[0]["item_id"], **ANSWER}]})

    def test_crash_before_the_marker_reruns_without_duplicates(self):
        self.all_sheet()
        answer_all(self.run_dir, "s_all")
        sheet.reveal(self.run_dir, "s_all")
        before = (self.run_dir / EXAM_ID / "verdicts.jsonl").read_bytes()
        (self.run_dir / "sheets/s_all/revealed.json").unlink()
        sheet.reveal(self.run_dir, "s_all")
        self.assertEqual((self.run_dir / EXAM_ID / "verdicts.jsonl").read_bytes(), before)

    def test_conflicting_verdict_for_a_presented_mechanism_is_an_error(self):
        self.all_sheet()
        answer_all(self.run_dir, "s_all")
        table = {row["mechanism_id"]: row for row in table_of(self.run_dir)["mechanisms"]}
        (self.run_dir / EXAM_ID / "verdicts.jsonl").write_text(json.dumps(verdict_line("m-001", table["m-001"]["idea_ids"])) + "\n")
        with self.assertRaisesRegex(ValueError, "different verdict"):
            sheet.reveal(self.run_dir, "s_all")

    def test_reveal_reports_mechanisms_added_after_the_sheet_was_issued(self):
        self.all_sheet()
        answer_all(self.run_dir, "s_all")
        table = table_of(self.run_dir)
        path = self.run_dir / EXAM_ID / B / "rep-2" / "ideas.jsonl"
        rows = read_rows(path)
        new = {**rows[0], "idea_id": "i-bbbbbbbbbbbb", "title": "新点子", "text": "出卷后才归入的。", "level": "round1", "batch": 2}
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows + [new]), encoding="utf-8")
        table["mechanisms"].append({"mechanism_id": "m-016", "label": "新机制", "idea_ids": [new["idea_id"]],
                                    "in_baseline": False, "representative": new["idea_id"]})
        (self.run_dir / EXAM_ID / "mechanisms.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        result = sheet.reveal(self.run_dir, "s_all")
        self.assertEqual(result["verdicts"], 15)
        self.assertEqual(result["unjudged_mechanisms"], ["m-016"])

    def test_group_sheet_reveals_files_that_really_calibrate(self):
        result = sheet.make_sheet(self.run_dir, {"id": EXAM_ID}, kind="group", sheet_id="s_grp", seed=3, count=8)
        self.assertEqual(result["counts"]["presented_target"], 8)
        key, public = key_of(self.run_dir, "s_grp"), public_of(self.run_dir, "s_grp")
        self.assertTrue(all(len(item["idea_ids"]) == 1 and item["mechanism_id"] is None for item in key["items"].values()))
        self.assertEqual({item["type"] for item in key["items"].values()}, {"target"})
        ids = [row["item_id"] for row in public["items"]]
        with self.assertRaisesRegex(ValueError, "Not finished"):
            sheet.merge_answers(self.run_dir, "s_grp", {"sheet_id": "s_grp", "groups": [ids[:3]]})
            sheet.reveal(self.run_dir, "s_grp")
        sheet.merge_answers(self.run_dir, "s_grp", {"sheet_id": "s_grp", "groups": [ids[:3], ids[3:]]})
        sheet.reveal(self.run_dir, "s_grp")
        root = self.run_dir / EXAM_ID
        human = json.loads((root / "human_groups.json").read_text())
        rows = read_rows(root / "human_group_ideas.jsonl")
        self.assertEqual(set(human), {"exam_id", "groups"})
        self.assertEqual(sorted(i for group in human["groups"] for i in group), sorted(r["idea_id"] for r in rows))
        self.assertEqual(len(rows), 8)
        report = dedupe.calibrate(rows, human, self.root / "calibration", exam_id=EXAM_ID, seed=1,
                                  backend=FakeBackend(responses=dedupe.fake_partition), ledger=self.ledger,
                                  model_a="m_one", model_b="m_two", allowed_models=["m_one", "m_two"], name="trial")
        self.assertEqual(report["status"], "complete")
        self.assertIsNotNone(report["ami"]["a_human"])

    def test_group_output_conflict_is_an_error(self):
        sheet.make_sheet(self.run_dir, {"id": EXAM_ID}, kind="group", sheet_id="s_grp", seed=3, count=4)
        ids = [row["item_id"] for row in public_of(self.run_dir, "s_grp")["items"]]
        sheet.merge_answers(self.run_dir, "s_grp", {"sheet_id": "s_grp", "groups": [ids]})
        (self.run_dir / EXAM_ID / "human_groups.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "different content"):
            sheet.reveal(self.run_dir, "s_grp")
        self.assertFalse((self.run_dir / "sheets/s_grp/revealed.json").exists())

    def test_group_sheet_needs_no_machine_partition_and_is_not_blocked_by_an_open_score_sheet(self):
        self.all_sheet()
        (self.run_dir / EXAM_ID / "mechanisms.json").unlink()
        result = sheet.make_sheet(self.run_dir, {"id": EXAM_ID}, kind="group", sheet_id="s_cold", seed=3, count=100)
        self.assertEqual(result["counts"]["presented_target"], 30)
        self.assertTrue(any("Only 30" in text for text in result["warnings"]))

    def test_group_sampling_is_seeded_and_ignores_machine_groups(self):
        def chosen(name, seed):
            run = copy_run(self.root, name)
            sheet.make_sheet(run, {"id": EXAM_ID}, kind="group", sheet_id="s", seed=seed, count=5)
            return [item["idea_ids"][0] for item in key_of(run, "s")["items"].values()]
        first = chosen("g1", 1)
        self.assertEqual(first, chosen("g2", 1))
        self.assertNotEqual(first, chosen("g3", 2))
        ideas = sorted(sheet.load_ideas(self.run_dir, EXAM_ID))
        random.Random("1:group").shuffle(ideas)
        self.assertEqual(set(first), set(ideas[:5]))


class GuardTests(SheetCase):
    def test_sheet_id_with_any_lineage_name_is_refused(self):
        for name in ("s_cfg_canary_a", "model_canary_x_1", "METHOD_CANARY_Y", "x-card_canary_1", "pressure3_sheet",
                     "round2", "baseline_only", "m-001", "s_m-001", next(iter(sheet.load_ideas(self.run_dir, EXAM_ID))),
                     "pressure_3", "pressure-3_x", "round_1", "round-2", "xcfg_canary_a", "cfg-canary-a"):
            with self.subTest(name), self.assertRaisesRegex(ValueError, "sheet_id must not contain"):
                self.all_sheet(name)
            self.assertNoSheet(name)

    def test_short_lineage_names_only_match_whole_tokens(self):
        terms = {"config": {"a"}, "model": set(), "method": set(), "card": set()}
        sheet.check_sheet_id("sample_sheet", terms)
        with self.assertRaises(ValueError):
            sheet.check_sheet_id("s_a", terms)

    def test_unsafe_and_reserved_names_are_refused(self):
        with self.assertRaises(ValueError):
            self.all_sheet("../escape")
        for name in sheet.RESERVED_EXAMS:
            with self.subTest(name), self.assertRaisesRegex(ValueError, "reserved"):
                sheet.make_sheet(self.run_dir, {"id": name}, kind="group", sheet_id="s", seed=1)

    def test_cli_rejects_mismatched_options(self):
        exams = ["--exam-dir", str(FIXTURE / "exams")]
        for extra in (["--kind", "group", "--all"], ["--kind", "score", *exams, "--configs", A], ["--kind", "score"],
                      ["--kind", "score", "--all", *exams, "--count", "5"],
                      ["--kind", "score", "--all", *exams, "--rewrite-config", "unused.json"]):
            result = subprocess.run([sys.executable, "-m", "evals.brainstorm", "sheet", "--run-dir", str(self.run_dir),
                                     "--exam-id", EXAM_ID, "--sheet-id", "s_cli", "--seed", "1", *extra],
                                    cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 1, extra)
            self.assertNoSheet("s_cli")

    def test_cli_output_is_one_json_line_without_item_mapping(self):
        result = subprocess.run([sys.executable, "-m", "evals.brainstorm", "sheet", "--kind", "score", "--all",
                                 "--run-dir", str(self.run_dir), "--exam-dir", str(FIXTURE / "exams"),
                                 "--exam-id", EXAM_ID, "--sheet-id", "s_cli", "--seed", "1"],
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertEqual(set(json.loads(result.stdout)), {"sheet_id", "counts", "warnings"})
        for secret in ("x-", "m-0", "i-", "canary"):
            self.assertNotIn(secret, result.stdout)

    def test_fixture_validates_against_the_real_readers(self):
        ideas = sheet.load_ideas(FIXTURE / "run", EXAM_ID)
        self.assertEqual(len(ideas), 30)
        self.assertEqual(len(sheet.load_table(FIXTURE / "run", EXAM_ID, ideas)["mechanisms"]), 15)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fixture_exam()["id"], EXAM_ID)
