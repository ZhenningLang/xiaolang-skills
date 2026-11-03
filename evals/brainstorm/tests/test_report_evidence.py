"""Assemble the report from a recorded fixture: blindness, constraint billing, cost and metric sections."""

import copy
from decimal import Decimal
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from evals.brainstorm import judge, report, sheet
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger
from evals.brainstorm.config import validate_config
from evals.brainstorm.exam import load_exams
from evals.brainstorm.record import read_rows


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "review_run"
EXAM_ID = "review_sample"
A, B = "cfg_canary_a", "cfg_canary_b"
ANSWER = {"unthought": True, "care": True, "fatal_reason": None, "want_to_try": False}


def configs():
    return [validate_config({"name": A, "default_model": "model_canary_x", "complexity": 1, "agents": 2}),
            validate_config({"name": B, "default_model": "model_canary_x", "complexity": 2, "agents": 2})]


class FixtureCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.run_dir = self.root / "run"
        shutil.copytree(FIXTURE / "run", self.run_dir)
        self.exam = load_exams(FIXTURE / "exams")[0]

    def build(self, **options):
        return report.build_report(self.run_dir, [self.exam], configs(), baseline=A, candidate=B,
                                   decision_exam_ids=[EXAM_ID], ledger_path=self.root / "absent.jsonl", **options)

    def all_ideas(self):
        return [row for path in sorted((self.run_dir / EXAM_ID).glob("*/rep-*/ideas.jsonl")) for row in read_rows(path)]

    def answer_and_reveal(self, sheet_id, **changes):
        items = json.loads((self.run_dir / "sheets" / sheet_id / "sheet.json").read_text(encoding="utf-8"))["items"]
        sheet.merge_answers(self.run_dir, sheet_id, {"sheet_id": sheet_id, "answers": [
            {"item_id": item["item_id"], **ANSWER, **changes} for item in items]})
        return sheet.reveal(self.run_dir, sheet_id)


class BlindReportTests(FixtureCase):
    def test_open_sheet_keeps_text_and_lineage_out_of_both_files(self):
        sheet.make_sheet(self.run_dir, self.exam, kind="score", sheet_id="s_all", seed=7, all_view=True)
        built = self.build()
        for name in ("report.md", "report.json"):
            text = (self.run_dir / name).read_text(encoding="utf-8")
            decoded = text if name == "report.md" else json.dumps(json.loads(text), ensure_ascii=False)
            for row in self.all_ideas():
                self.assertNotIn(row["text"], decoded, name)
                self.assertNotIn(row["idea_id"], decoded, name)
        self.assertEqual(built["unrevealed_sheets"], 1)
        self.assertIn("未还原的卷子：1 张", (self.run_dir / "report.md").read_text(encoding="utf-8"))
        self.assertEqual(built["exams"][EXAM_ID]["evidence"]["answers_present"], False)
        self.assertEqual(built["decision"]["decision"], "indeterminate")

    def test_after_reveal_every_merged_wording_is_expanded(self):
        sheet.make_sheet(self.run_dir, self.exam, kind="score", sheet_id="s_cmp", seed=7, configs=[A, B], per_unit=3,
                         backend=FakeBackend(responses=sheet.fake_rewrite), model="rewrite_model",
                         ledger=BudgetLedger(self.root / "rewrite.jsonl", "50"))
        self.answer_and_reveal("s_cmp")
        sheet.make_sheet(self.run_dir, self.exam, kind="score", sheet_id="s_all", seed=7, all_view=True)
        self.answer_and_reveal("s_all")
        built = self.build()
        text = (self.run_dir / "report.md").read_text(encoding="utf-8")
        table = json.loads((self.run_dir / EXAM_ID / "mechanisms.json").read_text(encoding="utf-8"))
        rows = {row["mechanism_id"]: row for row in built["exams"][EXAM_ID]["expansion"]}
        self.assertEqual(set(rows), {row["mechanism_id"] for row in table["mechanisms"]})
        for mechanism in table["mechanisms"]:
            self.assertEqual(sorted(row["idea_id"] for row in rows[mechanism["mechanism_id"]]["members"]), sorted(mechanism["idea_ids"]))
        merged = [row for row in table["mechanisms"] if len(row["idea_ids"]) > 1]
        self.assertTrue(merged, "the fixture must merge at least one mechanism")
        ideas = {row["idea_id"]: row for row in self.all_ideas()}
        for mechanism in merged:
            for key in mechanism["idea_ids"]:
                self.assertIn(" ".join(ideas[key]["text"].split()), text)
        self.assertEqual(built["unrevealed_sheets"], 0)

    def test_compare_only_sheet_expands_only_judged_mechanisms(self):
        sheet.make_sheet(self.run_dir, self.exam, kind="score", sheet_id="s_cmp", seed=7, configs=[A, B], per_unit=2,
                         backend=FakeBackend(responses=sheet.fake_rewrite), model="rewrite_model",
                         ledger=BudgetLedger(self.root / "rewrite.jsonl", "50"))
        self.answer_and_reveal("s_cmp")
        built = self.build()
        table = json.loads((self.run_dir / EXAM_ID / "mechanisms.json").read_text(encoding="utf-8"))["mechanisms"]
        judged = {row["mechanism_id"] for row in built["exams"][EXAM_ID]["expansion"]}
        self.assertLess(len(judged), len(table))
        members = {row["mechanism_id"]: row["idea_ids"] for row in table}
        unseen = {key for mechanism, ids in members.items() if mechanism not in judged for key in ids}
        text = (self.run_dir / "report.md").read_text(encoding="utf-8")
        for row in self.all_ideas():
            if row["idea_id"] in unseen:
                self.assertNotIn(row["text"], text)
        self.assertIn("还没判，不展开", text)

    def test_every_section_says_no_data_rather_than_zero_before_any_answer(self):
        built = self.build()
        text = (self.run_dir / "report.md").read_text(encoding="utf-8")
        sections = text.split("\n## ")[1:]
        self.assertEqual(len(sections), 13)
        for number, section in enumerate(sections, 1):
            self.assertTrue(section.startswith(f"{number}. "), section[:30])
            self.assertGreater(len(section.split("\n", 2)[2].strip()), 0, section[:30])
        self.assertIn("贡献度只说明相关，不说明因果", text)
        contribution = next(section for section in sections if section.startswith("4. "))
        self.assertIn("无数据", contribution)
        self.assertNotIn("0 / 0", contribution)
        self.assertEqual(built["exams"][EXAM_ID]["want"]["status"], "no_data")
        self.assertEqual(built["self_check"]["status"], "no_data")
        self.assertIn("暂定", text)


class ConstraintRouteTests(FixtureCase):
    def constrained(self):
        return {**self.exam, "constraints": ["不能拆掉架子"]}

    def test_judged_once_through_the_ledger(self):
        backend = FakeBackend(responses=judge.fake_judge)
        ledger = BudgetLedger(self.root / "ledger.jsonl", "50")
        first = report.judge_unit(self.run_dir, self.constrained(), A, 1, backend=backend, ledger=ledger, model="judge_m")
        again = report.judge_unit(self.run_dir, self.constrained(), A, 1, backend=backend, ledger=ledger, model="judge_m")
        self.assertEqual(first, again)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(sum(row["event"] == "settle" for row in read_rows(ledger.path)), 1)
        rows = read_rows(self.run_dir / EXAM_ID / A / "rep-1/ideas.jsonl")
        self.assertEqual(report.unit_constraint(self.run_dir, self.constrained(), A, 1, rows)["status"], "checked")

    def test_no_constraints_is_not_applicable_without_a_call(self):
        backend = FakeBackend(responses=judge.fake_judge)
        ledger = BudgetLedger(self.root / "ledger.jsonl", "50")
        result = report.judge_unit(self.run_dir, self.exam, A, 1, backend=backend, ledger=ledger, model="judge_m")
        self.assertEqual((result["status"], backend.calls), ("not_applicable", 0))
        self.assertEqual(report.unit_constraint(self.run_dir, self.exam, A, 1, [])["status"], "not_applicable")

    def test_unjudged_constraint_exam_blocks_acceptance(self):
        built = report.build_report(self.run_dir, [self.constrained()], configs(), baseline=A, candidate=B,
                                    decision_exam_ids=[EXAM_ID], ledger_path=self.root / "absent.jsonl")
        guard = built["exams"][EXAM_ID]["units"][A][1]["guards"]["constraint"]
        self.assertEqual((guard["status"], guard["violations"]), ("not_evaluated", None))
        self.assertEqual(built["decision"]["decision"], "indeterminate")

    def write_inputs(self):
        exams = self.root / "exams"
        shutil.copytree(FIXTURE / "exams", exams)
        path = exams / EXAM_ID / "exam.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["constraints"] = ["不能拆掉架子"]
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        paths = []
        for config in configs():
            target = self.root / f"{config['name']}.json"
            target.write_text(json.dumps({key: config[key] for key in ("name", "default_model", "complexity", "agents")}), encoding="utf-8")
            paths.append(target)
        judging = self.root / "judging.json"
        judging.write_text(json.dumps({"name": "judging", "default_model": "judge_m", "backend": {"kind": "fake"}}), encoding="utf-8")
        return exams, paths, judging

    def cli(self, exams, paths, ledger, *extra):
        command = [sys.executable, "-m", "evals.brainstorm", "report", "--run-dir", str(self.run_dir), "--exam-dir", str(exams),
                   "--baseline", A, "--candidate", B, "--decision-exams", EXAM_ID, "--ledger", str(ledger), *extra]
        for path in paths:
            command += ["--config", str(path)]
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_report_command_spends_only_with_judge_config(self):
        exams, paths, judging = self.write_inputs()
        ledger = self.root / "ledger.jsonl"
        ledger.write_text('{"event":"reserve","call_key":"held","usd":0.02,"tokens":{},"estimated":false,"pid":1,"ts":"t"}\n')
        before = ledger.read_bytes()
        done = self.cli(exams, paths, ledger)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(ledger.read_bytes(), before, "read-only mode must leave the ledger bytes alone")
        self.assertEqual(json.loads(done.stdout)["decision"], "indeterminate")
        self.assertTrue((self.run_dir / "report.md").is_file())
        paid = self.root / "paid.jsonl"
        done = self.cli(exams, paths, paid, "--judge-config", str(judging))
        self.assertEqual(done.returncode, 0, done.stderr)
        settled = [row for row in read_rows(paid) if row["event"] == "settle"]
        self.assertEqual(len(settled), 4)
        done = self.cli(exams, paths, paid, "--judge-config", str(judging))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len([row for row in read_rows(paid) if row["event"] == "settle"]), 4, "a rerun must not pay again")


class SectionTests(FixtureCase):
    def test_comparisons_not_evaluated_show_reason_never_zero(self):
        built = self.build()
        built = copy.deepcopy(built)
        built["exams"][EXAM_ID]["comparisons"] = {1: {
            "same_ideas": {"status": "not_evaluated", "reason": "点子不够比"},
            "same_tokens": {"status": "not_evaluated", "reason": "缺 token"}}}
        section = report.section_metrics(built)
        joined = "\n".join(section)
        self.assertIn("相同点子数：未评估（点子不够比）", joined)
        self.assertIn("相同 token：未评估（缺 token）", joined)
        self.assertIn("粗估", joined)

    def test_unevaluated_metric_cells_are_not_zero(self):
        built = self.build()
        result = built["exams"][EXAM_ID]["units"][A][1]
        result["metrics"]["unique_mechanisms"] = {"value": None, "status": "not_evaluated", "reason": "缺成员"}
        joined = "\n".join(report.section_metrics(built))
        self.assertIn("| unique_mechanisms | 未评估 |", joined)
        self.assertIn("缺成员", joined)

    def test_costs_show_fallback_paid_failures_and_estimates(self):
        built = self.build()
        call = {"backend": "claude", "purpose": "generate", "_scope": "recording", "call_key": "k"}
        receipts = [{**call, "call_key": "ok", "usd": Decimal("0.5"), "usd_estimated": True, "outcome": "ok"},
                    {**call, "call_key": "bad", "usd": Decimal("0.25"), "usd_estimated": False, "outcome": "paid_error"},
                    {**call, "call_key": "open", "purpose": "rewrite", "_scope": "sheet_rewrite", "usd": Decimal("0.125"),
                     "usd_estimated": True, "outcome": "uncertain"}]
        built["costs"] = report.cost_summary(receipts)
        built["exams"][EXAM_ID]["fallback_ids"] = 4
        joined = "\n".join(report.section_costs(built))
        self.assertIn("| 合计 | 3 | 0.875 | 0.625 | 1 次 / 0.25 | 1 |", joined)
        self.assertIn("| 用途 rewrite | 1 |", joined)
        self.assertIn(f"{EXAM_ID} = 4", joined)
        self.assertIn("不是已证实的新机制", joined)

    def test_reconcile_modes(self):
        receipts = [{"call_key": "a", "usd": Decimal("0.5"), "outcome": "ok"}]
        settled = {"a": {"event": "settle", "usd": Decimal("0.5")}, "other": {"event": "settle", "usd": Decimal("1")}}
        self.assertTrue(report.reconcile(receipts, settled)["ok"], "a shared ledger may hold other runs")
        exact = report.reconcile(receipts, settled, exact_match=True)
        self.assertEqual((exact["ok"], exact["settled_without_receipt"]), (False, ["other"]))
        self.assertEqual(report.reconcile(receipts, {"a": {"event": "settle", "usd": Decimal("0.4")}})["amount_mismatch"], ["a"])
        self.assertEqual(report.reconcile(receipts, {})["unsettled_receipts"], ["a"])
        self.assertTrue(report.reconcile(receipts * 2, settled)["duplicate_receipts"])

    def test_counts_section_claims_nothing_without_a_full_view(self):
        built = self.build()
        joined = "\n".join(report.section_counts(built))
        self.assertIn("无数据", joined)
        self.assertNotIn("全量卷都满足", joined)

    def test_failed_unit_refuses_the_comparison_sheet_as_the_readme_says(self):
        summary = self.run_dir / EXAM_ID / A / "rep-2/run_summary.json"
        raw = json.loads(summary.read_text(encoding="utf-8"))
        summary.write_text(json.dumps({**raw, "failures": 1}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            sheet.make_sheet(self.run_dir, self.exam, kind="score", sheet_id="s_cmp", seed=7, configs=[A, B], per_unit=2,
                             backend=FakeBackend(responses=sheet.fake_rewrite), model="rewrite_model",
                             ledger=BudgetLedger(self.root / "rewrite.jsonl", "50"))

    def test_read_ledger_only_reads(self):
        path = self.root / "ledger.jsonl"
        self.assertEqual(report.read_ledger(path), {})
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
