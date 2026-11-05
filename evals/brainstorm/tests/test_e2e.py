"""The whole chain on the fake backend, at a small scale so the suite stays usable."""

import contextlib
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evals.brainstorm import dedupe, e2e, replace_unit, replay, report, sheet
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetExceeded, BudgetLedger, LedgerError, cli_boundary, default_ledger_path, legacy_ledger_path
from evals.brainstorm.config import validate_config
from evals.brainstorm.exam import load_exams, read_jsonl, validate_exam
from evals.brainstorm.loop import run_loop
from evals.brainstorm.record import read_file, read_rows


ROOT = Path(__file__).resolve().parents[3]
REAL_LEDGER = legacy_ledger_path()
SMALL = {"exam_ids": ("open_sample",), "reps": 1}
SUMMARY_FIELDS = {"calls_total": int, "usd_total": (int, float, Decimal), "mechanisms_total": int, "presented_target": int,
                  "reused": int, "assignment_digest": str, "decision": str}
DECISIONS = {"accept", "reject", "indeterminate", "prefer_simpler"}
SECTION_TITLES = ("判定", "主分和噪声线", "护栏和成本", "贡献度", "留一对照", "失败账本", "计分规则自检",
                  "想试比例和计了分但不想试", "机器指标和两种比较口径", "费用", "条数核对", "校准", "被合并的说法", "单元替换")
# Picked by hand for open_sample at the default seed: the mean gap of 5/3 clears the noise line of 1.
# If the fake scenario changes, work out a new set from the units in that run's comparison key.
WINNERS = {"m-003", "m-011", "m-014", "m-016", "m-019"}
ACCEPT_SCORES = {"cfg_plain": [1, 2, 2], "cfg_return": [3, 4, 3]}


def quiet(function, *args, **options):
    """Progress lines are for a person at a terminal, not the test log."""
    with contextlib.redirect_stderr(io.StringIO()):
        return function(*args, **options)


def ledger_hash():
    return hashlib.sha256(REAL_LEDGER.read_bytes()).hexdigest() if REAL_LEDGER.exists() else None


def ensure(condition, message):
    if not condition:
        raise AssertionError(message)


def check_run(run_dir):
    """Everything a finished run must show; the phase check calls this on the default scale too."""
    root = Path(run_dir).resolve()
    summary = read_file(root / "e2e_summary.json")
    for field, kind in SUMMARY_FIELDS.items():
        ensure(isinstance(summary.get(field), kind) and not isinstance(summary.get(field), bool), f"summary field {field}")
    ensure(summary["decision"] in DECISIONS, "decision value")
    text = (root / "report.md").read_text(encoding="utf-8")
    sections = text.split("\n<summary>")[1:]
    ensure([section.split("</summary>", 1)[0] for section in sections]
           == [f"{number}. {title}" for number, title in enumerate(SECTION_TITLES, 1)], "report folds the 14 sections")
    for section in sections:
        ensure(section.split("</summary>", 1)[1].split("</details>", 1)[0].strip(), f"empty section: {section[:20]}")
    head = sections[0]
    ensure("只证明接线" in head and "样本小，只作方向判断" in head, "first section warns about the fake backend and sample size")
    built = read_file(root / "report.json")
    totals = {"mechanisms_total": 0, "presented_target": 0, "reused": 0}
    for exam_id, entry in built["exams"].items():
        counts = entry["counts"]
        ensure(counts["presented_target"] + counts["reused"] == counts["mechanisms_total"], f"conservation {exam_id}")
        ensure(not counts["new_after_issue"], f"unjudged mechanisms {exam_id}")
        for row in counts["sheets"]:
            ensure(row["masked_items"] == 0, f"source term was masked in {row['sheet_id']}")
            items = read_file(root / "sheets" / row["sheet_id"] / "sheet.json")["items"]
            ensure(len(items) == row["counts"]["presented_target"] + row["counts"]["check_bad"] + row["counts"]["repeat"],
                   f"item count {row['sheet_id']}")
        for field in totals:
            totals[field] += counts[field]
    for field, value in totals.items():
        ensure(summary[field] == value, f"summary total {field}")
    ensure(summary["mechanisms_total"] == summary["presented_target"] + summary["reused"], "total conservation")
    ensure(set(built["replay"]) == set(built["exams"]), "every exam has a replay")
    for exam_id, rows in built["replay"].items():
        for row in rows:
            ensure(row["status"] == "complete" and row["generation_calls"] == 0, f"replay finished without generating {exam_id}")
            ensure(row["samples"]["2"]["status"] == "ok", f"available replay size was evaluated {exam_id}")
            ensure(row["samples"]["5"]["status"] == "not_evaluated", f"short replay size stays unknown {exam_id}")
    if "open_sample" in built["exams"]:
        compare = read_file(root / "sheets/open_sample_compare/sheet.key.json")["counts"]
        ensure(compare["check_bad"] > 0, "titled check samples reach the comparison sheet")
    return summary


class Shared:
    """One small run the read-only tests share. Mutating tests work on a copy."""

    directory = None
    root = None
    summary = None
    calibration = {}
    answered_without_key = []
    replay_sources = {}

    @classmethod
    def build(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name).resolve() / "run"
        original_calibrate, original_simulate, original_replay = dedupe.calibrate, e2e.simulate_answers, replay.run_replay

        def calibrate(ideas, human, run_dir, **options):
            path = Path(run_dir) / options["exam_id"] / "mechanisms.json"
            before = path.read_bytes()
            result = original_calibrate(ideas, human, run_dir, **options)
            cls.calibration = {"before": before, "after": path.read_bytes()}
            return result

        def simulate(run_dir, sheet_id, seed):
            with patch.object(sheet, "load_key", side_effect=AssertionError("simulated answers must not read the key")):
                original_simulate(run_dir, sheet_id, seed)
            cls.answered_without_key.append(sheet_id)

        def run_replay(run_dir, exam, **options):
            sources = sorted(Path(run_dir, exam["id"]).glob("*/rep-*/ideas.jsonl")) + [Path(run_dir, exam["id"], "mechanisms.json")]
            before = [path.read_bytes() for path in sources]
            result = original_replay(run_dir, exam, **options)
            cls.replay_sources = {"before": before, "after": [path.read_bytes() for path in sources]}
            return result

        with (patch.object(dedupe, "calibrate", calibrate), patch.object(e2e, "simulate_answers", simulate),
              patch.object(replay, "run_replay", run_replay)):
            cls.summary = quiet(e2e.run_e2e, cls.root, **SMALL)


LEDGER_BEFORE = None


def setUpModule():
    global LEDGER_BEFORE
    LEDGER_BEFORE = ledger_hash()
    Shared.build()


def tearDownModule():
    Shared.directory.cleanup()
    if ledger_hash() != LEDGER_BEFORE:
        raise AssertionError("the end-to-end tests changed the real ledger")


class Workspace(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name).resolve()

    def copy(self, name="copy"):
        target = self.temp / name
        shutil.copytree(Shared.root, target)
        return target


class RunShapeTests(unittest.TestCase):
    def test_small_run_meets_the_full_check(self):
        check_run(Shared.root)

    def test_simulated_answers_used_public_state_only(self):
        self.assertEqual(set(Shared.answered_without_key), {"open_sample_group", "open_sample_compare", "open_sample_full"})

    def test_calibration_complete_and_authority_untouched(self):
        result = read_file(Shared.root / "replay/calibrate_open_sample/calibration.json")
        self.assertEqual(result["status"], "complete")
        self.assertIsNotNone(result["ami"]["a_human"])
        self.assertIsNotNone(result["ami"]["b_human"])
        self.assertEqual(Shared.calibration["before"], Shared.calibration["after"])

    def test_every_stage_appears_in_the_receipts(self):
        scopes = {row["_scope"] for row in report.load_receipts(Shared.root)}
        self.assertEqual(scopes, {"recording", "dedupe", "calibration", "replay", "constraint", "sheet_rewrite"})

    def test_replay_generates_nothing_and_leaves_sources_alone(self):
        rows = read_file(Shared.root / "report.json")["replay"]["open_sample"]
        self.assertEqual([(row["status"], row["generation_calls"]) for row in rows], [("complete", 0)])
        self.assertEqual(Shared.replay_sources["before"], Shared.replay_sources["after"])
        receipts = [row for row in report.load_receipts(Shared.root) if row["_scope"] == "replay"]
        self.assertTrue(receipts)
        self.assertFalse({row["purpose"] for row in receipts} & {"generate", "rewrite"})

    def test_report_shows_replay_sizes_one_by_one(self):
        section = (Shared.root / "report.md").read_text(encoding="utf-8").split("\n<summary>9.")[1].split("\n<summary>10.")[0]
        self.assertIn("回放", section)
        self.assertIn("生成调用 0 次", section)
        self.assertIn("5 个 agent：未评估（Insufficient recorded agents", section)
        self.assertRegex(section, r"2 个 agent：独有机制 \d+")

    def test_cost_sections_sum_to_the_ledger(self):
        built = read_file(Shared.root / "report.json")
        settled = [row for row in report.read_ledger(Shared.root / "ledger.fake.jsonl").values() if row["event"] == "settle"]
        total = sum(row["usd"] for row in settled)
        self.assertEqual(sum(entry["usd"] for entry in built["costs"]["by_purpose"].values()), total)
        self.assertEqual(sum(entry["usd"] for entry in built["costs"]["by_stage"].values()), total)
        self.assertGreater(built["costs"]["by_purpose"]["rewrite"]["calls"], 0)
        self.assertGreater(built["costs"]["by_stage"]["calibration"]["calls"], 0)
        self.assertEqual(built["receipts"]["mode"], "exact")
        self.assertEqual(Decimal(str(Shared.summary["usd_total"])), total)

    def test_report_covers_both_comparison_controls(self):
        built = read_file(Shared.root / "report.json")
        comparison = built["exams"]["open_sample"]["comparisons"]["1"]
        self.assertEqual(set(comparison), {"same_ideas", "same_tokens"})
        section = (Shared.root / "report.md").read_text(encoding="utf-8").split("\n<summary>9.")[1].split("\n<summary>10.")[0]
        self.assertIn("相同点子数", section)
        self.assertIn("相同 token", section)
        self.assertIn("粗估", section)

    def test_failure_ledger_finds_the_planted_symptoms(self):
        rows = read_file(Shared.root / "report.json")["failures"]
        found = {(row["config"], row["symptom"]): row for row in rows if row["config"]}
        self.assertGreater(found[("cfg_plain", "unused_cards")]["count"], 0)
        self.assertEqual(found[("cfg_plain", "return_same_mechanism")]["status"], "no_data")
        self.assertGreater(found[("cfg_return", "return_same_mechanism")]["count"], 0)
        self.assertEqual(found[("cfg_plain", "level_no_new")]["levels"], ["pressure1"])
        self.assertEqual(found[("cfg_plain", "subproblem_empty")]["status"], "no_data")

    def test_suspect_stage_gets_a_valid_leave_one_config(self):
        rows = read_file(Shared.root / "report.json")["leave_one"]
        by_stage = {(row["config"], row["stage"]): row for row in rows}
        pressure = by_stage[("cfg_plain", "level_pressure1")]
        self.assertEqual(pressure["changed"], {"levels": ["round1", "round2"]})
        self.assertEqual(pressure["drop_config"]["name"], "cfg_plain__drop_level_pressure1")


class CommandTests(Workspace):
    def test_fake_flag_is_mandatory(self):
        target = self.temp / "never"
        done = subprocess.run([sys.executable, "-m", "evals.brainstorm", "e2e", "--run-dir", str(target)], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertFalse(target.exists())

    def test_ledger_defaults_to_the_run_directory(self):
        self.assertEqual(Path(default_ledger_path("fake", Shared.root)), Shared.root / "ledger.fake.jsonl")
        self.assertNotEqual(REAL_LEDGER, Shared.root / "ledger.fake.jsonl")


class ConservationTests(Workspace):
    def edit_key(self, path, change):
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_dropped_row_while_issuing_stops_the_run(self):
        target = self.temp / "dropped"
        original = sheet.presentable
        with patch.object(sheet, "presentable", lambda selected, verdicts: original(selected, verdicts)[1:]):
            with self.assertRaisesRegex(ValueError, "Conservation"):
                quiet(e2e.run_e2e, target, **SMALL)
        self.assertFalse((target / "e2e_summary.json").exists())

    def test_removing_a_row_from_key_and_sheet_is_caught(self):
        target = self.copy()
        path = target / "sheets/open_sample_full"
        key = json.loads((path / "sheet.key.json").read_text(encoding="utf-8"))
        target_id = next(item for item, row in key["items"].items() if row["type"] == "target")
        self.edit_key(path / "sheet.key.json", lambda data: data["items"].pop(target_id))
        self.edit_key(path / "sheet.json", lambda data: data["items"].__setitem__(
            slice(None), [row for row in data["items"] if row["item_id"] != target_id]))
        with self.assertRaisesRegex(ValueError, "Conservation"):
            report.verify_counts(target, "open_sample")

    def test_consistently_lowered_counts_cannot_hide_a_lost_mechanism(self):
        target = self.copy()
        path = target / "sheets/open_sample_full"
        key = json.loads((path / "sheet.key.json").read_text(encoding="utf-8"))
        dropped = sorted(key["reused"])[0]

        def change(data):
            data["reused"].remove(dropped)
            for field in ("reused", "selected_target", "mechanisms_total"):
                data["counts"][field] -= 1
        self.edit_key(path / "sheet.key.json", change)
        with self.assertRaisesRegex(ValueError, "Conservation"):
            report.verify_counts(target, "open_sample")

    def cut_view(self, target, keep):
        """Keep only the mechanisms `keep` accepts in the full view, with every count lowered to match."""
        path = target / "sheets/open_sample_full"
        key = json.loads((path / "sheet.key.json").read_text(encoding="utf-8"))
        kept = {item for item, row in key["items"].items() if row["mechanism_id"] is None or keep(row["mechanism_id"])}

        def change(data):
            data["items"] = {item: data["items"][item] for item in kept}
            data["reused"] = [value for value in data["reused"] if keep(value)]
            kinds = [row["type"] for row in data["items"].values()]
            total = len(data["reused"]) + kinds.count("target")
            data["counts"].update(reused=len(data["reused"]), presented_target=kinds.count("target"), selected_target=total,
                                  mechanisms_total=total, repeat=kinds.count("repeat"), check_bad=kinds.count("check_bad"))
        self.edit_key(path / "sheet.key.json", change)
        self.edit_key(path / "sheet.json", lambda data: data.__setitem__(
            "items", [row for row in data["items"] if row["item_id"] in kept]))

    def highest_lost(self, target, *, revealed):
        path = target / "sheets/open_sample_full"
        key = json.loads((path / "sheet.key.json").read_text(encoding="utf-8"))
        limit = max(int(value[2:]) for value in key["reused"])
        self.cut_view(target, lambda value: int(value[2:]) < limit)
        if not revealed:
            (path / "revealed.json").unlink()

    def lose_one(self, target, sheet_id, kind):
        """Remove one item of a kind from key and page, with every count lowered to match. Units stay untouched."""
        path = target / "sheets" / sheet_id
        key = json.loads((path / "sheet.key.json").read_text(encoding="utf-8"))
        if kind == "reused":
            lost = sorted(key["reused"])[-1]

            def change(data):
                data["reused"].remove(lost)
                for field in ("reused", "selected_target", "mechanisms_total"):
                    data["counts"][field] -= 1
            self.edit_key(path / "sheet.key.json", change)
            return
        lost_item = sorted(item for item, row in key["items"].items() if row["type"] == kind)[-1]

        def change(data):
            del data["items"][lost_item]
            field = "presented_target" if kind == "target" else kind
            data["counts"][field] -= 1
            if kind == "target":
                data["counts"]["selected_target"] -= 1
                data["counts"]["mechanisms_total"] -= data["view"] == "all"
        self.edit_key(path / "sheet.key.json", change)
        self.edit_key(path / "sheet.json", lambda data: data.__setitem__(
            "items", [row for row in data["items"] if row["item_id"] != lost_item]))

    def test_losing_any_kind_of_row_from_any_sheet_is_caught(self):
        cases = [("open_sample_compare", "target"), ("open_sample_compare", "check_bad"), ("open_sample_compare", "repeat"),
                 ("open_sample_full", "target"), ("open_sample_full", "reused")]
        for sheet_id, kind in cases:
            for revealed in (True, False):
                with self.subTest(sheet=sheet_id, kind=kind, revealed=revealed):
                    target = self.copy(f"{sheet_id}_{kind}_{revealed}")
                    self.lose_one(target, sheet_id, kind)
                    if not revealed:
                        (target / "sheets" / sheet_id / "revealed.json").unlink()
                    with self.assertRaisesRegex(ValueError, "Conservation"):
                        report.verify_counts(target, "open_sample")

    def assertRefused(self, target, sheet_id):
        """Both doors refuse, and the reveal leaves no marker behind."""
        with self.assertRaisesRegex(ValueError, "Conservation"):
            report.verify_counts(target, "open_sample")
        marker = target / "sheets" / sheet_id / "revealed.json"
        marker.unlink(missing_ok=True)
        with self.assertRaisesRegex(ValueError, "Conservation"):
            sheet.reveal(target, sheet_id)
        self.assertFalse(marker.exists())

    def later_compare(self, name):
        """A comparison sheet issued once every verdict exists, which is how a real sheet comes to hold reused rows."""
        target = self.copy(name)
        exam = next(row for row in load_exams(e2e.EXAM_DIR) if row["id"] == "open_sample")
        sheet.make_sheet(target, exam, kind="score", sheet_id="open_sample_later", seed=e2e.DEFAULT_SEED + 1,
                         configs=["cfg_plain", "cfg_return"])
        path = target / "sheets/open_sample_later"
        self.assertGreater(read_file(path / "sheet.key.json")["counts"]["reused"], 0)
        return target, path

    def mechanism_outside(self, target, path):
        """A real mechanism of this exam that the sheet never issued: nothing but the issued record can tell."""
        selected = set(read_file(path / sheet.ISSUED_NAME)["selected"])
        table = read_file(target / "open_sample/mechanisms.json")["mechanisms"]
        return next(row for row in table if row["mechanism_id"] not in selected)

    def test_compare_reused_row_and_unit_slot_lost_with_matching_counts(self):
        target, path = self.later_compare("reused_and_slot")
        key = read_file(path / "sheet.key.json")
        lost = sorted(set(key["reused"]).intersection(*map(set, key["units"].values())))[-1]

        def change(data):
            data["reused"].remove(lost)
            for rows in data["units"].values():
                rows.remove(lost)
            for field in ("reused", "selected_target"):
                data["counts"][field] -= 1
        self.edit_key(path / "sheet.key.json", change)
        self.assertRefused(target, "open_sample_later")

    def test_full_sheet_losing_a_check_sample_is_caught(self):
        target = self.copy()
        self.lose_one(target, "open_sample_full", "check_bad")
        self.assertRefused(target, "open_sample_full")

    def test_item_pointed_at_another_valid_mechanism_is_caught(self):
        target = self.copy()
        path = target / "sheets/open_sample_compare"
        other = self.mechanism_outside(target, path)

        def change(data):
            item = next(row for row in data["items"].values() if row["type"] == "target")
            item.update(mechanism_id=other["mechanism_id"], idea_ids=other["idea_ids"])
        self.edit_key(path / "sheet.key.json", change)
        self.assertRefused(target, "open_sample_compare")

    def test_reused_row_swapped_for_another_valid_mechanism_is_caught(self):
        target, path = self.later_compare("reused_swap")
        other = self.mechanism_outside(target, path)["mechanism_id"]
        self.edit_key(path / "sheet.key.json", lambda data: data["reused"].__setitem__(0, other))
        self.assertRefused(target, "open_sample_later")

    def test_unit_slot_swapped_for_another_valid_mechanism_is_caught(self):
        target = self.copy()
        path = target / "sheets/open_sample_compare"
        other = self.mechanism_outside(target, path)["mechanism_id"]
        self.edit_key(path / "sheet.key.json", lambda data: next(iter(data["units"].values())).__setitem__(0, other))
        self.assertRefused(target, "open_sample_compare")

    def test_count_no_equation_reads_is_still_caught(self):
        target = self.copy()
        self.edit_key(target / "sheets/open_sample_compare/sheet.key.json",
                      lambda data: data["counts"].__setitem__("mechanisms_total", data["counts"]["mechanisms_total"] + 1))
        self.assertRefused(target, "open_sample_compare")

    def test_damage_before_reveal_stops_the_reveal(self):
        target = self.copy()
        path = target / "sheets/open_sample_full"
        (path / "revealed.json").unlink()
        self.lose_one(target, "open_sample_full", "target")
        with self.assertRaisesRegex(ValueError, "Conservation"):
            sheet.reveal(target, "open_sample_full")
        self.assertFalse((path / "revealed.json").exists())

    def test_missing_issued_record_is_refused(self):
        target = self.copy()
        (target / "sheets/open_sample_compare" / sheet.ISSUED_NAME).unlink()
        with self.assertRaisesRegex(ValueError, "Conservation"):
            report.verify_counts(target, "open_sample")

    def test_issued_record_holds_every_id_and_matches_the_sheet(self):
        for sheet_id in ("open_sample_compare", "open_sample_full"):
            path = Shared.root / "sheets" / sheet_id
            record = read_file(path / sheet.ISSUED_NAME)
            key = read_file(path / "sheet.key.json")
            self.assertEqual(set(record["items"]), set(key["items"]))
            targets = {row["mechanism_id"] for row in record["items"].values() if row["type"] == "target"}
            self.assertEqual(targets | set(record["reused"]), set(record["selected"]))
        compare = read_file(Shared.root / "sheets/open_sample_compare" / sheet.ISSUED_NAME)
        self.assertEqual(set(compare["selected"]), {value for rows in compare["units"].values() for value in rows})
        full = read_file(Shared.root / "sheets/open_sample_full" / sheet.ISSUED_NAME)
        table = read_file(Shared.root / "open_sample/mechanisms.json")["mechanisms"]
        self.assertEqual(set(full["selected"]), {row["mechanism_id"] for row in table})

    def test_page_never_sees_the_issued_record(self):
        state = json.dumps(sheet.public_state(Shared.root, "open_sample_full"), ensure_ascii=False)
        self.assertNotIn("mechanism_id", state)
        self.assertNotRegex(state, r"m-\d{3}")

    def test_highest_mechanism_lost_after_reveal_is_caught(self):
        target = self.copy()
        self.highest_lost(target, revealed=True)
        with self.assertRaisesRegex(ValueError, "Conservation"):
            report.verify_counts(target, "open_sample")

    def test_highest_judged_mechanism_lost_before_reveal_is_caught(self):
        target = self.copy()
        self.highest_lost(target, revealed=False)
        with self.assertRaisesRegex(ValueError, "Conservation"):
            report.verify_counts(target, "open_sample")

    def test_open_sheet_losing_its_highest_unjudged_row_is_caught(self):
        target = self.copy()
        (target / "sheets/open_sample_full/revealed.json").unlink()
        table = json.loads((target / "open_sample/mechanisms.json").read_text(encoding="utf-8"))
        highest = max(row["mechanism_id"] for row in table["mechanisms"])
        verdicts = target / "open_sample/verdicts.jsonl"
        verdicts.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in read_rows(verdicts)
                                    if row["mechanism_id"] != highest), encoding="utf-8")
        self.cut_view(target, lambda value: value != highest)
        with self.assertRaisesRegex(ValueError, "Conservation"):
            report.verify_counts(target, "open_sample")

    def test_changed_counts_are_caught(self):
        for field, delta in (("presented_target", -1), ("reused", 1), ("mechanisms_total", 1), ("check_bad", 1)):
            with self.subTest(field=field):
                target = self.copy(f"counts_{field}")
                self.edit_key(target / "sheets/open_sample_full/sheet.key.json",
                              lambda data: data["counts"].__setitem__(field, data["counts"][field] + delta))
                with self.assertRaisesRegex(ValueError, "Conservation"):
                    report.verify_counts(target, "open_sample")

    def test_sheet_without_full_view_is_no_data(self):
        with self.assertRaises(ValueError):
            report.verify_counts(self.temp / "missing", "../bad")
        target = self.copy()
        shutil.rmtree(target / "sheets/open_sample_full")
        self.assertEqual(report.verify_counts(target, "open_sample")["status"], "no_data")

    def test_late_mechanism_is_reported_not_refused(self):
        target = self.copy()
        path = target / "open_sample/mechanisms.json"
        table = json.loads(path.read_text(encoding="utf-8"))
        moved = next(row for row in table["mechanisms"] if len(row["idea_ids"]) > 1)
        table["mechanisms"].append({"mechanism_id": "m-900", "label": "后来的", "idea_ids": [moved["idea_ids"].pop()],
                                    "representative": None, "in_baseline": False})
        table["mechanisms"][-1]["representative"] = table["mechanisms"][-1]["idea_ids"][0]
        moved["representative"] = moved["idea_ids"][0]
        path.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(report.verify_counts(target, "open_sample")["new_after_issue"], ["m-900"])


class AcceptPathTests(Workspace):
    """Fake-backend products and fixed answers, through the real sheets and report, reach accept and what overrides it."""

    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.source = Path(temp.name).resolve() / "run"
        original = e2e.simulate_answers

        def answer(run_dir, sheet_id, seed):
            key = read_file(Path(run_dir) / "sheets" / sheet_id / "sheet.key.json")
            if key["kind"] == "group":
                original(run_dir, sheet_id, seed)
                return
            sheet.merge_answers(run_dir, sheet_id, {"sheet_id": sheet_id, "answers": [
                {"item_id": item_id, "unthought": item["mechanism_id"] in WINNERS, "care": item["mechanism_id"] in WINNERS,
                 "fatal_reason": None, "want_to_try": False} for item_id, item in key["items"].items()]})

        with patch.object(e2e, "simulate_answers", answer):
            cls.summary = quiet(e2e.run_e2e, cls.source, exam_ids=("open_sample",), reps=3)

    def copy(self, name="copy"):
        target = self.temp / name
        shutil.copytree(self.source, target)
        return target

    def rebuild(self, root, **options):
        exams = [exam for exam in load_exams(e2e.EXAM_DIR) if exam["id"] == "open_sample"]
        configs = [validate_config(read_file(root / "e2e_inputs" / f"{name}.json")) for name in ("cfg_plain", "cfg_return")]
        return quiet(report.build_report, root, exams, configs, baseline="cfg_plain", candidate="cfg_return",
                     decision_exam_ids=["open_sample"], seed=e2e.DEFAULT_SEED, ledger_path=root / "ledger.fake.jsonl",
                     exact_receipts=True, **options)["decision"]

    def add_constraint_violation(self, root):
        """Edit the candidate's real judge result: one idea it generated now breaks the constraint."""
        unit = root / "open_sample/cfg_return/rep-1"
        produced = {row["idea_id"] for row in read_rows(unit / "ideas.jsonl") if row["level"] != "baseline"}
        path = unit / "judges/constraint/result.json"
        result = json.loads(path.read_text(encoding="utf-8"))
        next(row for row in result["verdicts"] if row["idea_id"] in produced).update(violation=True, reason="Blocks the shelf.")
        result["violations"] += 1
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    def test_fixed_answers_reach_accept(self):
        self.assertEqual((self.summary["decision"], self.summary["preferred"]), ("accept", "cfg_return"))
        built = read_file(self.source / "report.json")
        self.assertEqual(built["decision"]["matched_row"], 4)
        self.assertEqual(built["exams"]["open_sample"]["evidence"]["scores"], ACCEPT_SCORES)
        check_run(self.source)
        # The control for the tests below: rebuilding the same products changes nothing on its own.
        self.assertEqual(self.rebuild(self.copy())["matched_row"], 4)

    def test_cost_over_limit_turns_accept_into_reject(self):
        decision = self.rebuild(self.copy(), cost_ratio="1.4")
        self.assertEqual((decision["decision"], decision["matched_row"]), ("reject", 3), decision["reason"])
        self.assertIn("成本", decision["reason"])

    def test_more_constraint_violations_turn_accept_into_reject(self):
        target = self.copy()
        self.add_constraint_violation(target)
        decision = self.rebuild(target)
        self.assertEqual((decision["decision"], decision["matched_row"]), ("reject", 3), decision["reason"])
        self.assertIn("硬约束", decision["reason"])

    def test_cost_and_constraints_together_still_reject(self):
        target = self.copy()
        self.add_constraint_violation(target)
        decision = self.rebuild(target, cost_ratio="1.4")
        self.assertEqual((decision["decision"], decision["matched_row"]), ("reject", 3), decision["reason"])


class HiddenFailureRowsTests(Workspace):
    def test_open_sheet_hides_idea_ids_in_the_built_report(self):
        target = self.copy()
        (target / "sheets/open_sample_full/revealed.json").unlink()
        exams = [exam for exam in load_exams(e2e.EXAM_DIR) if exam["id"] == "open_sample"]
        configs = [read_file(target / "e2e_inputs" / f"{name}.json") for name in ("cfg_plain", "cfg_return")]
        built = quiet(report.build_report, target, exams, [validate_config(row) for row in configs], baseline="cfg_plain",
                      candidate="cfg_return", decision_exam_ids=["open_sample"], ledger_path=target / "ledger.fake.jsonl")
        rows = [row for row in built["failures"] if row["config"]]
        self.assertTrue(any(row["idea_ids_hidden"] for row in rows), "the run must have hits to hide")
        self.assertTrue(all(row["idea_ids"] == [] and not row.get("unknown_ids") for row in rows))
        # Only these rows: mechanisms judged on an already revealed sheet may show their lineage in the expansion.
        self.assertNotRegex(json.dumps(rows, default=str), r"i-[0-9a-f]{12}")


class CrashRecoveryTests(Workspace):
    """Pins what the README promises: reconciling a crashed call keeps the bill right, does not resume the work,
    and leaves one recorded replacement as the way forward."""

    def setUp(self):
        super().setUp()
        self.exam = next(row for row in load_exams(e2e.EXAM_DIR) if row["id"] == "parts_sample")
        self.root = self.temp / "crash"
        self.root.mkdir()
        *configs, self.classifier = e2e.write_inputs(self.root)
        self.config = configs[0]
        self.ledger = BudgetLedger(self.root / "ledger.fake.jsonl", "50")
        self.generation = FakeBackend(responses=e2e.scenario)
        self.grouping = FakeBackend(responses=dedupe.fake_partition)

    def run_unit(self, generation, grouping):
        quiet(run_loop, self.exam, self.config, self.root, seed=1, backend=generation, ledger=self.ledger,
              dedupe_config=self.classifier, dedupe_backend=grouping, rep=1)

    def uncertain_key(self):
        keys = [key for key, row in report.read_ledger(self.ledger.path).items() if row["event"] == "uncertain"]
        self.assertEqual(len(keys), 1)
        return keys[0]

    def crash(self, generation, grouping):
        with self.assertRaises(LedgerError):
            self.run_unit(generation, grouping)
        return self.uncertain_key()

    def replace(self):
        return quiet(replace_unit.replace_unit, self.exam, self.config, self.root, failed_rep=1, new_rep=4, backend=self.generation,
                     ledger=self.ledger, dedupe_config=self.classifier, dedupe_backend=self.grouping)

    def assertReplaced(self, key, stage, events):
        """The bill stays, the receipt matches it, the new unit finishes, and there is no second replacement."""
        result = self.replace()
        self.assertEqual((result["status"], result["stage"], result["failed_usd"] >= Decimal("0.01")), ("complete", stage, True))
        after = read_rows(self.ledger.path)
        self.assertEqual(after[:len(events)], events)
        self.assertEqual(report.read_ledger(self.ledger.path)[key]["usd"], Decimal("0.01"))
        self.assertTrue(report.reconcile(report.load_receipts(self.root), report.read_ledger(self.ledger.path))["ok"])
        self.assertEqual(sheet.discover_units(self.root, "parts_sample", "cfg_plain"), [("cfg_plain", 4)])
        with self.assertRaisesRegex(ValueError, "replaced"):
            self.run_unit(self.generation, self.grouping)

    def test_settled_recording_call_stops_the_unit(self):
        key = self.crash(FakeBackend(failure="unknown"), self.grouping)
        self.ledger.settle(key, Decimal("0.01"))
        with self.assertRaisesRegex(ValueError, "Generation failed"):
            self.run_unit(self.generation, self.grouping)
        self.assertReplaced(key, "recording", read_rows(self.ledger.path))

    def test_settled_recording_call_is_replaceable_without_resuming_first(self):
        key = self.crash(FakeBackend(failure="unknown"), self.grouping)
        self.ledger.settle(key, Decimal("0.01"))
        self.assertReplaced(key, "recording", read_rows(self.ledger.path))

    def test_settled_later_stage_call_keeps_refusing(self):
        key = self.crash(self.generation, FakeBackend(failure="unknown"))
        self.ledger.settle(key, Decimal("0.01"))
        with self.assertRaisesRegex(LedgerError, "already paid"):
            self.run_unit(self.generation, self.grouping)
        self.assertReplaced(key, "grouping", read_rows(self.ledger.path))

    def test_uncertain_call_cannot_be_replaced_before_it_is_reconciled(self):
        self.crash(FakeBackend(failure="unknown"), self.grouping)
        with self.assertRaisesRegex(ValueError, "Reconcile"):
            self.replace()

    def test_released_call_resumes(self):
        key = self.crash(FakeBackend(failure="unknown"), self.grouping)
        self.ledger.release(key, confirm_not_charged=True)
        self.run_unit(self.generation, self.grouping)
        self.assertTrue((self.root / "parts_sample/cfg_plain/rep-1/run_summary.json").is_file())


class SeedTests(Workspace):
    def test_seed_behaviour(self):
        first = self.temp / "first"
        summary = quiet(e2e.run_e2e, first, **SMALL)
        for field in ("assignment_digest", "decision", "calls_total", "usd_total"):
            self.assertEqual(summary[field], Shared.summary[field], field)
        events = (first / "ledger.fake.jsonl").read_bytes()
        again = quiet(e2e.run_e2e, first, **SMALL)
        self.assertEqual(again["calls_total"], summary["calls_total"])
        self.assertEqual((first / "ledger.fake.jsonl").read_bytes(), events, "a rerun must not touch the ledger")
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            quiet(e2e.run_e2e, first, seed=e2e.DEFAULT_SEED + 1, **SMALL)
        self.assertEqual((first / "ledger.fake.jsonl").read_bytes(), events, "a changed seed must not mix in")
        other = quiet(e2e.run_e2e, self.temp / "other", seed=e2e.DEFAULT_SEED + 1, **SMALL)
        self.assertNotEqual(other["assignment_digest"], summary["assignment_digest"])


class BudgetStopTests(Workspace):
    def stage_limits(self):
        """A limit that lets everything before the chosen call settle and stops that call's reservation."""
        receipts = report.load_receipts(Shared.root)
        scope = {row["call_key"]: row["_scope"] for row in receipts}
        settled = [row for row in read_rows(Shared.root / "ledger.fake.jsonl") if row["event"] == "settle"]
        first, spent = {}, Decimal(0)
        seen = {}
        for row in settled:
            stage = scope[row["call_key"]]
            seen[stage] = seen.get(stage, 0) + 1
            # The third recording call lands inside a batch, not at the very start of the run.
            if stage not in first and seen[stage] == (3 if stage == "recording" else 1):
                first[stage] = (spent, row["call_key"])
            spent += row["usd"]
        self.assertEqual(set(first), {"recording", "dedupe", "calibration", "replay", "constraint", "sheet_rewrite"})
        return {stage: (before + Decimal("0.015"), key) for stage, (before, key) in first.items()}

    def snapshot(self, root):
        return {str(path.relative_to(root)): read_rows(path) for path in root.rglob("ideas.jsonl")}

    def test_stop_and_resume_at_every_stage(self):
        for stage, (limit, stop_key) in self.stage_limits().items():
            with self.subTest(stage=stage):
                target = self.temp / stage
                code = quiet(cli_boundary, lambda _: e2e.run_e2e(target, budget_usd=str(limit), **SMALL), None)
                self.assertEqual(code, 3)
                self.assertFalse((target / "e2e_summary.json").exists())
                events = read_rows(target / "ledger.fake.jsonl")
                self.assertTrue(all(row["event"] != "uncertain" for row in events))
                self.assertLessEqual(sum(row["usd"] for row in events if row["event"] == "settle"), limit)
                ideas = self.snapshot(target)
                summary = quiet(e2e.run_e2e, target, **SMALL)
                for field in ("calls_total", "usd_total", "assignment_digest", "decision"):
                    self.assertEqual(summary[field], Shared.summary[field], field)
                after = read_rows(target / "ledger.fake.jsonl")
                self.assertEqual(after[:len(events)], events, "events before the stop stay a prefix")
                settled = [row["call_key"] for row in after if row["event"] == "settle"]
                self.assertEqual(len(settled), len(set(settled)), "every key settles once")
                self.assertEqual(report.reconcile(report.load_receipts(target), report.read_ledger(target / "ledger.fake.jsonl"),
                                                  exact_match=True)["open_events"], [])
                for name, rows in ideas.items():
                    self.assertEqual(read_rows(target / name)[:len(rows)], rows, name)

    def test_stop_raises_budget_error_at_the_boundary(self):
        with self.assertRaises(BudgetExceeded):
            quiet(e2e.run_e2e, self.temp / "tiny", budget_usd="0.001", **SMALL)


class ReceiptTests(Workspace):
    def test_deleted_receipt_fails_the_exact_check(self):
        target = self.copy()
        path = target / "sheets/open_sample_compare/calls.jsonl"
        rows = path.read_text(encoding="utf-8").splitlines(keepends=True)
        self.assertTrue(rows)
        path.write_text("".join(rows[1:]), encoding="utf-8")
        check = report.reconcile(report.load_receipts(target), report.read_ledger(target / "ledger.fake.jsonl"), exact_match=True)
        self.assertFalse(check["ok"])
        self.assertEqual(len(check["settled_without_receipt"]), 1)

    def test_all_receipts_equal_the_fake_ledger(self):
        check = report.reconcile(report.load_receipts(Shared.root), report.read_ledger(Shared.root / "ledger.fake.jsonl"), exact_match=True)
        self.assertTrue(check["ok"])
        self.assertEqual(check["receipts"], Shared.summary["calls_total"])


class InjectionTests(Workspace):
    def test_marked_response_is_rejected(self):
        marker = "INJECTION_CANARY_42"
        target = self.temp / "marked"
        summary = quiet(e2e.run_e2e, target, exam_ids=("injection_sample",), reps=3, marker=marker)
        self.assertEqual(summary["decision"], "reject")
        built = read_file(target / "report.json")
        self.assertEqual(built["decision"]["matched_row"], 2)
        self.assertTrue(built["decision"]["known_injection_violations"])
        self.assertIn("注入告警", (target / "report.md").read_text(encoding="utf-8"))


class SampleTests(unittest.TestCase):
    def test_all_samples_load_and_open_sample_has_titles(self):
        exams = {exam["id"]: exam for exam in load_exams(e2e.EXAM_DIR)}
        self.assertEqual(len(exams), 9)
        samples = exams["open_sample"]["check_samples"]
        self.assertEqual(len(samples), 3)
        for text in samples:
            self.assertGreater(sum(bool(line.strip()) for line in text.splitlines()), 1)

    def test_validator_neither_loosened_nor_tightened(self):
        folder = e2e.EXAM_DIR / "open"
        raw = json.loads((folder / "exam.json").read_text(encoding="utf-8"))
        cards = read_jsonl(folder / "cards.jsonl")
        validate_exam({**raw, "check_samples": ["only one line"]}, cards)
        with self.assertRaises(ValueError):
            validate_exam({**raw, "check_samples": [3]}, cards)


class ReadmeTests(unittest.TestCase):
    def test_usage_guide(self):
        text = (ROOT / "evals/README.md").read_text(encoding="utf-8")
        for needle in ("e2e --fake", "--budget-usd", "exit code 3", "worst case", "evals/brainstorm/runs/", "Not Verified",
                       "title line", "--judge-config", "--decision-exams", "only proves the wiring", "replay",
                       "bypass the page", "agent session", "cannot be retried", "replace-unit", "Do not release a call"):
            self.assertIn(needle, text)
        self.assertNotIn("not an exam runner", text)


if __name__ == "__main__":
    unittest.main()
