"""The acceptance table and diagnostics as pure functions on hand-written inputs."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals.brainstorm import report
from evals.brainstorm.config import validate_config
from evals.brainstorm.record import digest


OLD, NEW = "cfg_old", "cfg_new"
POLICY = {"baseline": OLD, "candidate": NEW, "cost_ratio": "1.5"}
COMPLEXITY = {OLD: 1, NEW: 2}
NA = {"status": "not_applicable", "violations": None}
UNKNOWN = {"status": "not_evaluated", "violations": None}
FLAT = (2, 2, 2)


def guard(injection=NA, constraint=NA):
    return {"injection": injection, "constraint": constraint}


def row(exam_id="e1", old=FLAT, new=FLAT, *, old_cost=1, new_cost=1, old_guard=None, new_guard=None, answers=True, reason=None):
    return {"exam_id": exam_id, "answers_present": answers, "answers_reason": reason,
            "scores": {OLD: list(old), NEW: list(new)},
            "guards": {OLD: [old_guard or guard() for _ in old], NEW: [new_guard or guard() for _ in new]},
            "cost_usd": {OLD: [old_cost for _ in old], NEW: [new_cost for _ in new]}}


def decide(rows, ids=None, complexity=COMPLEXITY, policy=POLICY):
    return report.decide(rows, decision_exam_ids=[r["exam_id"] for r in rows] if ids is None else ids,
                         policy=policy, complexity=complexity)


class DecisionTableTests(unittest.TestCase):
    def assertRow(self, result, decision, matched):
        self.assertEqual((result["decision"], result["matched_row"]), (decision, matched), result["reason"])

    def test_row1_no_decision_exams(self):
        self.assertRow(decide([row()], ids=[]), "indeterminate", 1)

    def test_row1_fewer_than_half_judgeable(self):
        rows = [row("e1"), row("e2", (1, 2), (1, 2)), row("e3", (1, 2), (1, 2))]
        self.assertRow(decide(rows), "indeterminate", 1)

    def test_row1_missing_answers_names_the_reason(self):
        result = decide([row(answers=False, reason="没有比较卷")])
        self.assertRow(result, "indeterminate", 1)
        self.assertIn("没有比较卷", result["reason"])

    def test_row1_named_exam_without_any_record(self):
        result = decide([row()], ids=["e1", "ghost"])
        self.assertRow(result, "indeterminate", 1)
        self.assertIn("ghost", result["reason"])

    def test_row1_unknown_guard_is_never_zero(self):
        for field in ("injection", "constraint"):
            with self.subTest(field=field):
                self.assertRow(decide([row(new_guard=guard(**{field: UNKNOWN}))]), "indeterminate", 1)
                self.assertRow(decide([row(new_guard=guard(**{field: {"status": "not_evaluated", "violations": 0}}))]),
                               "indeterminate", 1)

    def test_row1_missing_cost(self):
        self.assertRow(decide([row(new_cost=None)]), "indeterminate", 1)

    def test_row1_guard_for_each_repetition(self):
        short = row()
        short["guards"][NEW] = short["guards"][NEW][:2]
        self.assertRow(decide([short]), "indeterminate", 1)

    def test_row2_injection_in_decision_exam(self):
        seen = {"status": "checked", "violations": 2}
        result = decide([row(new_guard=guard(injection=seen))])
        self.assertRow(result, "reject", 2)
        self.assertEqual(result["known_injection_violations"],
                         [{"exam_id": "e1", "config": NEW, "rep_index": index, "violations": 2} for index in (1, 2, 3)])

    def test_row2_injection_outside_decision_exams(self):
        other = row("outside", new_guard=guard(injection={"status": "checked", "violations": 1}))
        self.assertRow(decide([row("e1"), other], ids=["e1"]), "reject", 2)

    def test_row2_known_positive_with_incomplete_coverage(self):
        partial = {"status": "not_evaluated", "violations": 1}
        self.assertRow(decide([row(new_guard=guard(injection=partial))]), "reject", 2)

    def test_row3_more_hard_constraint_violations(self):
        result = decide([row(old_guard=guard(constraint={"status": "checked", "violations": 0}),
                             new_guard=guard(constraint={"status": "checked", "violations": 1}))])
        self.assertRow(result, "reject", 3)
        self.assertEqual(result["preferred"], OLD)

    def test_incomplete_constraint_stops_at_the_first_row_and_keeps_its_positive(self):
        result = decide([row(new_guard=guard(constraint={"status": "not_evaluated", "violations": 1}))])
        self.assertRow(result, "indeterminate", 1)
        self.assertEqual(result["known_constraint_violations"],
                         [{"exam_id": "e1", "config": NEW, "rep_index": index, "violations": 1} for index in (1, 2, 3)])

    def test_partial_constraint_count_cannot_support_accept(self):
        partial = guard(constraint={"status": "not_evaluated", "violations": 1})
        cases = {"candidate partial below baseline": ({"status": "checked", "violations": 2}, partial["constraint"]),
                 "candidate partial equal to baseline": ({"status": "checked", "violations": 1}, partial["constraint"]),
                 "both partial": (partial["constraint"], partial["constraint"]),
                 "baseline partial above candidate": ({"status": "not_evaluated", "violations": 3}, {"status": "checked", "violations": 1})}
        for name, (old, new) in cases.items():
            with self.subTest(name):
                result = decide([row(old=FLAT, new=(9, 9, 9), old_guard=guard(constraint=old), new_guard=guard(constraint=new))])
                self.assertRow(result, "indeterminate", 1)
                self.assertIn("硬约束", result["reason"])

    def test_partial_baseline_cannot_prove_a_rise(self):
        result = decide([row(old_guard=guard(constraint={"status": "not_evaluated", "violations": 1}),
                             new_guard=guard(constraint={"status": "checked", "violations": 5}))])
        self.assertRow(result, "indeterminate", 1)

    def test_known_lower_bound_above_baseline_is_still_missing_evidence(self):
        result = decide([row(old_guard=guard(constraint={"status": "checked", "violations": 1}),
                             new_guard=guard(constraint={"status": "not_evaluated", "violations": 2}))])
        self.assertRow(result, "indeterminate", 1)

    def test_table_order_incomplete_constraint_comes_before_cost(self):
        partial = guard(constraint={"status": "not_evaluated", "violations": 1})
        result = decide([row(new_cost=2, old_guard=guard(constraint={"status": "checked", "violations": 1}), new_guard=partial)])
        self.assertRow(result, "indeterminate", 1)

    def test_row3_cost_over_limit(self):
        self.assertRow(decide([row(new_cost="1.51")]), "reject", 3)

    def test_row3_cost_over_limit_outranks_a_better_score(self):
        result = decide([row(old=FLAT, new=(9, 9, 9), new_cost="1.51")])
        self.assertRow(result, "reject", 3)
        self.assertEqual(result["preferred"], OLD)
        self.assertIn("成本", result["reason"])

    def test_row3_more_violations_outrank_a_better_score(self):
        result = decide([row(old=FLAT, new=(9, 9, 9), old_guard=guard(constraint={"status": "checked", "violations": 0}),
                             new_guard=guard(constraint={"status": "checked", "violations": 1}))])
        self.assertRow(result, "reject", 3)
        self.assertEqual(result["preferred"], OLD)
        self.assertIn("硬约束", result["reason"])

    def test_row4_accept(self):
        result = decide([row(old=FLAT, new=(9, 9, 9))])
        self.assertRow(result, "accept", 4)
        self.assertEqual(result["preferred"], NEW)

    def test_row5_reject(self):
        self.assertRow(decide([row(old=(9, 9, 9), new=FLAT)]), "reject", 5)

    def test_row6_prefer_simpler(self):
        result = decide([row()])
        self.assertRow(result, "prefer_simpler", 6)
        self.assertEqual(result["preferred"], OLD)


class DecisionBoundaryTests(unittest.TestCase):
    def test_under_three_repetitions_is_only_unjudgeable(self):
        result = decide([row("e1"), row("e2", (1, 2), (1, 2))])
        self.assertEqual(result["matched_row"], 6)
        self.assertEqual(result["judgeable_exams"], ["e1"])
        self.assertFalse(result["exams"]["e2"]["judgeable"])
        self.assertIsNone(result["exams"]["e2"]["noise"])

    def test_exactly_half_judgeable_is_not_indeterminate(self):
        self.assertNotEqual(decide([row("e1"), row("e2", (1,), (1,))])["decision"], "indeterminate")

    def test_exactly_half_above_noise_is_neither_accept_nor_reject(self):
        better = decide([row("e1", FLAT, (9, 9, 9)), row("e2")])
        self.assertEqual(better["matched_row"], 6)
        worse = decide([row("e1", (9, 9, 9), FLAT), row("e2")])
        self.assertEqual(worse["matched_row"], 6)

    def test_difference_equal_to_noise_is_not_above_it(self):
        # Thirds on purpose: rounded decimals would call this just under or just over the line.
        result = decide([row(old=(1, 1, 2), new=(2, 2, 3))])
        self.assertEqual(result["exams"]["e1"]["noise"], 1)
        self.assertEqual(result["matched_row"], 6)
        self.assertEqual(decide([row(old=(2, 2, 3), new=(1, 1, 2))])["matched_row"], 6)

    def test_cost_exactly_at_limit_is_allowed(self):
        self.assertEqual(decide([row(new_cost="1.5")])["matched_row"], 6)

    def test_zero_baseline_cost(self):
        self.assertEqual(decide([row(old_cost=0, new_cost=0)])["matched_row"], 6)
        self.assertEqual(decide([row(old_cost=0, new_cost="0.01")])["matched_row"], 3)

    def test_cost_is_summed_over_decision_exams_and_shown_per_exam(self):
        result = decide([row("e1", new_cost=2), row("e2", new_cost=1)])
        self.assertEqual(result["matched_row"], 6)
        self.assertEqual(result["totals"]["cost_usd"], {OLD: 2, NEW: 3})

    def test_table_order_missing_answers_with_injection(self):
        seen = guard(injection={"status": "checked", "violations": 1})
        result = decide([row(answers=False, reason="缺", new_guard=seen)])
        self.assertEqual(result["decision"], "indeterminate")
        self.assertEqual(sum(entry["violations"] for entry in result["known_injection_violations"]), 3)

    def test_complexity_states(self):
        self.assertEqual(decide([row()], complexity={OLD: 3, NEW: 2})["preferred"], NEW)
        self.assertEqual(decide([row()], complexity={OLD: 2, NEW: 2})["preferred"], OLD)
        missing = decide([row()], complexity={OLD: 2})
        self.assertEqual((missing["decision"], missing["preferred"]), ("prefer_simpler", None))
        self.assertTrue(missing["warnings"])

    def test_pure_no_files_no_input_changes(self):
        rows = [row("e1", FLAT, (9, 9, 9)), row("e2", new_guard=guard(injection={"status": "checked", "violations": 1}))]
        before = deepcopy(rows)
        with patch("builtins.open", side_effect=AssertionError("decide must not read files")):
            decide(rows)
        self.assertEqual(rows, before)


class MainScoreTests(unittest.TestCase):
    def verdict(self, **changes):
        return {"unthought": True, "care": True, "fatal_reason": None, "want_to_try": False, **changes}

    def test_scores_follow_units_not_config_reps(self):
        key = {"units": {f"{OLD}/rep-1": ["m-001", "m-002"], f"{NEW}/rep-1": ["m-002", "m-003"]},
               "items": {"x-1": {"type": "target", "mechanism_id": "m-003", "config_reps": [f"{OLD}/rep-1", f"{NEW}/rep-1"]}}}
        result = report.main_scores(key, {"m-001": self.verdict(), "m-002": self.verdict(care=False), "m-003": self.verdict()})
        self.assertEqual(result["units"], {f"{OLD}/rep-1": 1, f"{NEW}/rep-1": 1})

    def test_reused_verdicts_count_and_want_to_try_does_not(self):
        key = {"units": {"a/rep-1": ["m-001", "m-002", "m-003"]}, "reused": ["m-001"]}
        verdicts = {"m-001": self.verdict(), "m-002": self.verdict(want_to_try=True),
                    "m-003": self.verdict(want_to_try=True, fatal_reason="有原因")}
        self.assertEqual(report.main_scores(key, verdicts)["units"]["a/rep-1"], 2)

    def test_small_sample_warns_and_unequal_units_refuse(self):
        rows = [f"m-{n:03d}" for n in range(1, 10)]
        verdicts = {key: self.verdict() for key in rows}
        self.assertTrue(report.main_scores({"units": {"a/rep-1": rows}}, verdicts)["warnings"])
        rows.append("m-010")
        verdicts["m-010"] = self.verdict()
        self.assertFalse(report.main_scores({"units": {"a/rep-1": rows}}, verdicts)["warnings"])
        with self.assertRaisesRegex(ValueError, "Conservation"):
            report.main_scores({"units": {"a/rep-1": rows, "b/rep-1": rows[:9]}}, verdicts)

    def test_missing_verdict_refuses(self):
        with self.assertRaisesRegex(ValueError, "Conservation"):
            report.main_scores({"units": {"a/rep-1": ["m-001"]}}, {})

    def test_zero_or_two_comparison_sheets_count_as_missing_answers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertIsNone(report.find_compare_sheet(root, "e1", (OLD, NEW))[0])

            def issue(name, *, revealed=True, units=None):
                path = root / "sheets" / name
                path.mkdir(parents=True)
                (path / "sheet.key.json").write_text(json.dumps({
                    "exam_id": "e1", "view": "compare", "kind": "score",
                    "units": units or {f"{OLD}/rep-1": [], f"{NEW}/rep-1": []}}))
                if revealed:
                    (path / "revealed.json").write_text("{}")

            issue("open_one", revealed=False)
            key, reason = report.find_compare_sheet(root, "e1", (OLD, NEW))
            self.assertIsNone(key)
            self.assertIn("未还原 1", reason)
            issue("other_configs", units={f"{OLD}/rep-1": [], "third/rep-1": []})
            self.assertIsNone(report.find_compare_sheet(root, "e1", (OLD, NEW))[0])
            issue("first")
            self.assertEqual(report.find_compare_sheet(root, "e1", (OLD, NEW))[1], None)
            issue("second")
            key, reason = report.find_compare_sheet(root, "e1", (OLD, NEW))
            self.assertIsNone(key)
            self.assertIn("2 张", reason)


def idea(key, level="round1", *, config=OLD, returned=False, method=None, cards_used=("c1",), cards=("c1",), model="m", origin=None):
    return {"idea_id": key, "config": config, "rep": 1, "level": level, "returned": returned, "method": method,
            "cards": list(cards), "cards_used": list(cards_used), "model": model, "returned_from": origin,
            "agent": 1, "title": "题 " + key, "text": "文 " + key}


def table_of(*groups):
    return {"mechanisms": [{"mechanism_id": f"m-{index:03d}", "label": f"机制{index}", "idea_ids": list(group)}
                           for index, group in enumerate(groups, 1)]}


def verdict(*, scored=True, want=False):
    return {"unthought": scored, "care": True, "fatal_reason": None, "want_to_try": want, "label": "判断时的标签"}


class ConstraintGuardTests(unittest.TestCase):
    ROWS = [idea("a", "baseline"), idea("b"), idea("c")]

    def result(self, *rows, inputs=("a", "b", "c")):
        return {"status": "checked", "input_ids": list(inputs),
                "verdicts": [{"idea_id": key, "violation": flag, "reason": "r" if flag else ""} for key, flag in rows]}

    def test_counts_only_nonbaseline(self):
        guard_result = report.constraint_guard(self.result(("a", True), ("b", True), ("c", False)), self.ROWS)
        self.assertEqual((guard_result["status"], guard_result["violations"]), ("checked", 1))

    def test_clean_complete_is_checked_zero(self):
        guard_result = report.constraint_guard(self.result(("a", False), ("b", False), ("c", False)), self.ROWS)
        self.assertEqual((guard_result["status"], guard_result["violations"]), ("checked", 0))

    def test_file_cannot_declare_itself_not_applicable(self):
        # Only the exam decides that, in unit_constraint. A result file saying so is damaged or edited.
        self.assertEqual(report.constraint_guard({"status": "not_applicable", "violations": None}, self.ROWS)["status"], "not_evaluated")

    def test_one_bad_row_spoils_the_whole_result(self):
        good = [{"idea_id": key, "violation": False, "reason": ""} for key in ("a", "b", "c")]
        base = {"status": "checked", "input_ids": ["a", "b", "c"]}
        cases = {"bad duplicate": [*good, {"idea_id": "b", "violation": True}],
                 "bad foreign": [*good, {"idea_id": "z"}],
                 "not an object": [*good, "row"]}
        for name, rows in cases.items():
            with self.subTest(name):
                guard_result = report.constraint_guard({**base, "verdicts": rows}, self.ROWS)
                self.assertEqual((guard_result["status"], guard_result["violations"]), ("not_evaluated", None))
                result = decide([row(old=FLAT, new=(9, 9, 9), old_guard=guard(constraint={"status": "checked", "violations": 2}),
                                     new_guard=guard(constraint=guard_result))])
                self.assertEqual(result["decision"], "indeterminate")

    def test_valid_positive_survives_a_bad_row(self):
        rows = [{"idea_id": "a", "violation": False, "reason": ""}, {"idea_id": "b", "violation": True, "reason": "越界"},
                {"idea_id": "c"}]
        guard_result = report.constraint_guard({"status": "checked", "input_ids": ["a", "b", "c"], "verdicts": rows}, self.ROWS)
        self.assertEqual((guard_result["status"], guard_result["violations"]), ("not_evaluated", 1))

    def test_missing_result_is_not_evaluated(self):
        self.assertEqual(report.constraint_guard(None, self.ROWS)["violations"], None)

    def test_incomplete_coverage_is_unknown_and_keeps_known_positive(self):
        cases = {"missing": self.result(("a", False), ("b", False), inputs=("a", "b")),
                 "repeated": self.result(("a", False), ("b", False), ("b", False), ("c", False)),
                 "foreign": self.result(("a", False), ("b", False), ("c", False), ("z", False), inputs=("a", "b", "c", "z"))}
        for name, result in cases.items():
            with self.subTest(name=name):
                guard_result = report.constraint_guard(result, self.ROWS)
                self.assertEqual((guard_result["status"], guard_result["violations"]), ("not_evaluated", None))
        positive = report.constraint_guard(self.result(("a", False), ("b", True), inputs=("a", "b")), self.ROWS)
        self.assertEqual((positive["status"], positive["violations"]), ("not_evaluated", 1))

    def test_verdict_without_a_valid_answer_is_not_a_clean_check(self):
        base = {"status": "checked", "input_ids": ["a", "b", "c"]}
        complete = [{"idea_id": "a", "violation": False, "reason": ""}, {"idea_id": "b", "violation": False, "reason": ""}]
        for name, last in {"missing boolean": {"idea_id": "c"}, "string boolean": {"idea_id": "c", "violation": "false", "reason": ""},
                           "integer boolean": {"idea_id": "c", "violation": 0, "reason": ""},
                           "missing reason": {"idea_id": "c", "violation": False},
                           "violation without reason": {"idea_id": "c", "violation": True, "reason": " "}}.items():
            with self.subTest(name):
                guard_result = report.constraint_guard({**base, "verdicts": [*complete, last]}, self.ROWS)
                self.assertEqual((guard_result["status"], guard_result["violations"]), ("not_evaluated", None))

    def test_bad_constraint_file_cannot_reach_accept(self):
        bad = report.constraint_guard({"status": "checked", "input_ids": ["a", "b", "c"],
                                       "verdicts": [{"idea_id": "a"}, {"idea_id": "b"}, {"idea_id": "c"}]}, self.ROWS)
        self.assertEqual(decide([row(old=FLAT, new=(9, 9, 9), new_guard=guard(constraint=bad))])["decision"], "indeterminate")

    def test_partial_result_with_known_positive_reaches_indeterminate(self):
        partial = report.constraint_guard(self.result(("a", False), ("b", True), inputs=("a", "b")), self.ROWS)
        result = decide([row(old=FLAT, new=(9, 9, 9), old_guard=guard(constraint={"status": "checked", "violations": 2}),
                             new_guard=guard(constraint=partial))])
        self.assertEqual(result["decision"], "indeterminate")
        self.assertEqual(sum(entry["violations"] for entry in result["known_constraint_violations"]), 3)

    def test_incomplete_coverage_reaches_indeterminate(self):
        partial = report.constraint_guard(self.result(("a", False), inputs=("a",)), self.ROWS)
        result = decide([row(new_guard=guard(constraint=partial))])
        self.assertEqual(result["decision"], "indeterminate")


def config(**changes):
    return validate_config({"name": OLD, "default_model": "m", "complexity": 1, **changes})


class ContributionTests(unittest.TestCase):
    def test_hand_counted_sample(self):
        ideas = {row["idea_id"]: row for row in (
            idea("only_one"), idea("only_one_b"),  # all round1: counts for round1
            idea("across_a"), idea("across_b", "round2"),  # crosses levels: counts for neither
            idea("with_base", "baseline"), idea("with_base_b"),  # baseline member: never counts
            idea("second", "round2"),  # all round2
            idea("unscored"),  # no scored verdict: outside the denominator
            idea("other_config", "pressure1", config=NEW))}  # another config's member is ignored here
        table = table_of(["only_one", "only_one_b"], ["across_a", "across_b"], ["with_base", "with_base_b"],
                         ["second", "other_config"], ["unscored"])
        verdicts = {"m-001": verdict(), "m-002": verdict(), "m-003": verdict(), "m-004": verdict(), "m-005": verdict(scored=False)}
        result = report.contribution(config(levels=["round1", "round2"]), ideas, table, verdicts)
        rows = {entry["stage"]: entry for entry in result["stages"]}
        self.assertEqual(result["denominator"], 4)
        self.assertEqual((rows["level_round1"]["numerator"], rows["level_round1"]["denominator"]), (1, 4))
        self.assertEqual(rows["level_round1"]["share"], Decimal("0.25"))
        self.assertEqual(rows["level_round2"]["numerator"], 1)
        # Baseline members never pass: the mechanism would have appeared without any later stage.
        self.assertEqual(rows["materials"]["numerator"], 3)
        self.assertEqual(rows["model_m"]["numerator"], 3)

    def test_other_config_member_does_not_block_this_config(self):
        ideas = {row["idea_id"]: row for row in (idea("a"), idea("b", "baseline", config=NEW))}
        result = report.contribution(config(), ideas, table_of(["a", "b"]), {"m-001": verdict()})
        self.assertEqual(result["stages"][0]["numerator"], 1)

    def test_no_verdicts_is_no_data_not_zero(self):
        result = report.contribution(config(), {"a": idea("a")}, table_of(["a"]), {})
        self.assertEqual((result["status"], result["stages"], result["denominator"]), ("no_data", [], None))
        self.assertEqual(report.contribution(config(), {}, None, {})["status"], "no_data")

    def test_suspect_needs_presence_denominator_and_low_share(self):
        ideas = {row["idea_id"]: row for row in (idea("a"), idea("b", "round2"), idea("c"), idea("c_b", "round2"))}
        table = table_of(["a"], ["b"], ["c", "c_b"])
        current = config(levels=["round1", "round2", "pressure1"])
        judged = {"m-001": verdict(scored=False), "m-002": verdict(scored=False), "m-003": verdict()}
        result = report.contribution(current, ideas, table, judged)
        rows = {entry["stage"]: entry for entry in result["stages"]}
        self.assertEqual((rows["level_round1"]["numerator"], rows["level_round1"]["suspect"]), (0, True))
        self.assertFalse(rows["level_pressure1"]["suspect"], "no idea came through this stage")
        self.assertFalse(report.contribution(current, ideas, table, {"m-001": verdict(scored=False)})["stages"][0]["suspect"])
        raised = report.contribution(current, ideas, table, {"m-001": verdict(), "m-003": verdict()}, floor="0.5")
        self.assertTrue({entry["stage"]: entry for entry in raised["stages"]}["level_round1"]["suspect"])


class UnjudgedStageTests(unittest.TestCase):
    IDEAS = {row["idea_id"]: row for row in (idea("a"), idea("b", "round2"))}
    TABLE = table_of(["a"], ["b"])
    CURRENT = config(levels=["round1", "round2"])

    def stages(self, verdicts):
        return {entry["stage"]: entry for entry in report.contribution(self.CURRENT, self.IDEAS, self.TABLE, verdicts)["stages"]}

    def test_stage_with_unjudged_mechanisms_is_not_suspect(self):
        rows = self.stages({"m-001": verdict()})
        self.assertEqual((rows["level_round2"]["numerator"], rows["level_round2"]["unjudged"]), (0, 1))
        self.assertFalse(rows["level_round2"]["suspect"], "nobody judged it yet, so a zero says nothing")
        self.assertEqual(rows["level_round1"]["unjudged"], 0)

    def test_judged_but_unscored_stage_stays_suspect(self):
        rows = self.stages({"m-001": verdict(), "m-002": verdict(scored=False)})
        self.assertTrue(rows["level_round2"]["suspect"])
        self.assertEqual(rows["level_round2"]["unjudged"], 0)

    def test_unjudged_stage_gets_no_leave_one_config(self):
        contributions = {"e1": {OLD: report.contribution(self.CURRENT, self.IDEAS, self.TABLE, {"m-001": verdict()})}}
        self.assertEqual(report.leave_one_rows([self.CURRENT], contributions), [])

    def test_sections_name_the_unjudged_stage(self):
        result = report.contribution(self.CURRENT, self.IDEAS, self.TABLE, {"m-001": verdict()})
        built = {"exams": {"e1": {"contribution": {OLD: result}}}, "configs": [self.CURRENT], "leave_one": [],
                 "policy": {"contribution_floor": Decimal(0)}}
        self.assertIn("未判定 1 条", "\n".join(report.section_contribution(built)))
        section = "\n".join(report.section_leave_one(built))
        self.assertIn("未判定 1 条", section)
        self.assertNotIn("无数据", section)


class LeaveOneTests(unittest.TestCase):
    def differ(self, original, built):
        return {key for key in {*original, *built} if key != "name" and original.get(key) != built.get(key)}

    def test_each_stage_differs_in_one_place_and_validates(self):
        original = config(levels=["round1", "round2"], return_collisions=True, method_mode="different",
                          methods={"a": "x", "b": "y"}, models=["m", "n"])
        expected = {("level", "round1"): {"levels"}, ("return", None): {"return_collisions"}, ("method", "a"): {"methods"},
                    ("materials", None): {"materials_mode"}, ("model", "n"): {"models"}}
        for (kind, value), fields in expected.items():
            with self.subTest(kind=kind):
                built = report.leave_one_config(original, "x", kind, value)
                self.assertEqual(self.differ(original, built["drop_config"]), fields)
                self.assertEqual(built["drop_config"]["name"], f"{OLD}__drop_x")
                self.assertEqual(validate_config(built["drop_config"]), built["drop_config"])

    def test_last_level_becomes_baseline_only(self):
        built = report.leave_one_config(config(levels=["round1"]), "x", "level", "round1")
        self.assertEqual(built["drop_config"]["levels"], ["baseline"])

    def test_last_method_turns_methods_off(self):
        built = report.leave_one_config(config(method_mode="same", methods={"a": "x"}), "x", "method", "a")
        self.assertEqual((built["drop_config"]["method_mode"], built["drop_config"]["methods"]), ("none", {}))

    def test_only_model_cannot_be_removed(self):
        built = report.leave_one_config(config(), "x", "model", "m")
        self.assertIsNone(built["drop_config"])
        self.assertIn("只有一个模型", built["reason"])

    def test_removing_default_model_moves_the_default(self):
        built = report.leave_one_config(config(models=["m", "n"]), "x", "model", "m")
        self.assertEqual((built["drop_config"]["default_model"], built["drop_config"]["models"]), ("n", ["n"]))

    def test_every_suspect_stage_listed_and_unrecorded_switches_named(self):
        current = config(three_step=True, decomposition_timing="late")
        contributions = {"e1": {OLD: {"stages": [
            {"stage": "level_round1", "label": "级别 round1", "kind": "level", "value": "round1", "suspect": True},
            {"stage": "materials", "label": "材料", "kind": "materials", "value": None, "suspect": False}]}},
            "e2": {OLD: {"stages": [
                {"stage": "level_round1", "label": "级别 round1", "kind": "level", "value": "round1", "suspect": True}]}}}
        rows = report.leave_one_rows([current], contributions)
        by_stage = {entry["stage"]: entry for entry in rows}
        self.assertEqual(set(by_stage), {"level_round1", "switch_three_step", "switch_decomposition_timing"})
        self.assertEqual(by_stage["level_round1"]["exams"], ["e1", "e2"])
        for name in ("switch_three_step", "switch_decomposition_timing"):
            self.assertIn("算不了贡献度", by_stage[name]["note"])
        self.assertIs(by_stage["switch_three_step"]["drop_config"]["three_step"], False)
        self.assertEqual(by_stage["switch_decomposition_timing"]["drop_config"]["decomposition_timing"], "none")


class FailureLedgerTests(unittest.TestCase):
    UNIT = {"exam_id": "e1", "config": OLD, "rep": 1}
    GROWTH = {"baseline": {"value": None, "status": "not_evaluated", "reason": "参照"}, "round1": {"value": 2, "status": "ok"}}

    def rows(self, ideas, table=None, *, fallback=(), growth=None, levels=("round1",), annotation=None, **options):
        output = report.failure_rows(self.UNIT, ideas, table, fallback, self.GROWTH if growth is None else growth,
                                     list(levels), annotation, **options)
        return {entry["symptom"]: entry for entry in output}

    def assertNoData(self, entry):
        self.assertEqual((entry["status"], entry["count"], entry["denominator"]), ("no_data", None, None))
        self.assertTrue(entry["reason"])

    def test_every_symptom_points_at_its_stage(self):
        rows = self.rows([idea("a")])
        self.assertEqual({key: value["stage"] for key, value in rows.items()},
                         {"unused_cards": "材料分配或提示词", "return_same_mechanism": "去重判定或退回提示",
                          "level_no_new": "压力升级或级别设置", "subproblem_empty": "拆法汇总或指派"})

    def test_unused_cards_hit_miss_no_data(self):
        hit = self.rows([idea("a", cards_used=()), idea("b"), idea("base", "baseline", cards_used=())])["unused_cards"]
        self.assertEqual((hit["status"], hit["count"], hit["denominator"], hit["idea_ids"]), ("ok", 1, 2, ["a"]))
        miss = self.rows([idea("a")])["unused_cards"]
        self.assertEqual((miss["count"], miss["denominator"]), (0, 1))
        self.assertNoData(self.rows([idea("a", cards=(), cards_used=())])["unused_cards"])

    def test_return_same_mechanism_hit_miss_no_data_unknown(self):
        origin = idea("orig")
        same = idea("back_a", returned=True, origin="orig")
        changed = idea("back_b", returned=True, origin="orig")
        table = table_of(["orig", "back_a"], ["back_b"])
        hit = self.rows([origin, same, changed], table)["return_same_mechanism"]
        self.assertEqual((hit["count"], hit["denominator"], hit["idea_ids"]), (1, 2, ["back_a"]))
        miss = self.rows([origin, changed], table_of(["orig"], ["back_b"]))["return_same_mechanism"]
        self.assertEqual((miss["count"], miss["denominator"]), (0, 1))
        self.assertNoData(self.rows([origin], table_of(["orig"]))["return_same_mechanism"])
        self.assertNoData(self.rows([origin, same], None)["return_same_mechanism"])
        unsure = self.rows([origin, same, changed], table, fallback=["back_a"])["return_same_mechanism"]
        self.assertEqual((unsure["count"], unsure["denominator"], unsure["unknown_ids"]), (0, 1, ["back_a"]))
        everything = self.rows([origin, same], table, fallback=["orig"])["return_same_mechanism"]
        self.assertNoData(everything)
        self.assertEqual(everything["unknown_ids"], ["back_a"])

    def test_level_without_new_mechanisms_hit_miss_no_data(self):
        growth = {"round1": {"value": 3, "status": "ok"}, "round2": {"value": 0, "status": "ok"},
                  "pressure1": {"value": None, "status": "not_evaluated", "reason": "缺批次"}}
        hit = self.rows([idea("a")], growth=growth, levels=("round1", "round2", "pressure1"))["level_no_new"]
        self.assertEqual((hit["count"], hit["denominator"], hit["levels"]), (1, 2, ["round2"]))
        self.assertEqual(hit["not_evaluated"], {"pressure1": "缺批次"})
        miss = self.rows([idea("a")], growth=growth, levels=("round1",))["level_no_new"]
        self.assertEqual((miss["count"], miss["denominator"]), (0, 1))
        self.assertNoData(self.rows([idea("a")], growth=growth, levels=("pressure1",))["level_no_new"])

    def test_subproblem_hit_miss_no_data(self):
        annotation = {"exam_id": "e1", "targets": ["甲", "乙"], "matches": {"a": ["甲"]}}
        hit = self.rows([idea("a")], annotation=annotation)["subproblem_empty"]
        self.assertEqual((hit["count"], hit["denominator"], hit["targets"]), (1, 2, ["乙"]))
        full = {"exam_id": "e1", "targets": ["甲"], "matches": {"a": ["甲"]}}
        self.assertEqual(self.rows([idea("a")], annotation=full)["subproblem_empty"]["count"], 0)
        self.assertNoData(self.rows([idea("a")])["subproblem_empty"])
        self.assertNoData(self.rows([idea("a"), idea("b")], annotation=annotation)["subproblem_empty"])

    def test_incomplete_unit_never_reports_zero(self):
        rows = self.rows([idea("a")], completion={"status": "not_evaluated", "reason": "录制失败"})
        for entry in rows.values():
            self.assertNoData(entry)
            self.assertEqual(entry["reason"], "录制失败")

    def test_four_manual_symptoms_listed(self):
        calibration = {"e1": {"difference_counts": {"a": {"false_merges": 2, "false_splits": 0}}}}
        rows = {entry["symptom"]: entry for entry in report.manual_rows(calibration)}
        self.assertEqual({key: (entry["stage"], entry["status"]) for key, entry in rows.items()},
                         {"method_same_mechanism": ("方法写得不够具体", "manual"), "dedupe_false_merge": ("去重", "manual"),
                          "odd_after_exhaustion": ("压力升级缺少约束", "manual"), "judge_author_gap": ("评委", "manual")})
        self.assertEqual(rows["dedupe_false_merge"]["reference"]["e1"]["a"]["false_merges"], 2)


class HiddenLineageTests(unittest.TestCase):
    def test_every_idea_list_is_hidden_while_a_sheet_is_open(self):
        rows = [{"idea_ids": ["i-1"], "unknown_ids": ["i-2"]}, {"idea_ids": [], "unknown_ids": ["i-3"]}, {"idea_ids": []}]
        report.hide_idea_ids(rows)
        self.assertEqual(rows, [{"idea_ids": [], "unknown_ids": [], "idea_ids_hidden": True},
                                {"idea_ids": [], "unknown_ids": [], "idea_ids_hidden": True},
                                {"idea_ids": [], "idea_ids_hidden": False}])


class SelfCheckTests(unittest.TestCase):
    def results(self, scored_count, total, **repeat):
        return {"s1": {"check_bad": {"total": total, "scored": scored_count},
                       "repeat": {"total": 0, "consistent": 0, "inconsistent": 0, **repeat}}}

    def test_threshold_is_strict_and_configurable(self):
        self.assertIsNone(report.rule_self_check(self.results(1, 3))["warning"], "one of three stays under the default")
        self.assertIsNotNone(report.rule_self_check(self.results(2, 3))["warning"])
        self.assertIsNone(report.rule_self_check(self.results(1, 2), "0.5")["warning"], "equal is not over")
        self.assertIsNotNone(report.rule_self_check(self.results(1, 2), "0.49")["warning"])

    def test_threshold_marked_provisional(self):
        self.assertEqual(report.rule_self_check(self.results(0, 3))["threshold_status"], "暂定")

    def test_no_samples_is_no_data(self):
        output = report.rule_self_check({})
        self.assertEqual((output["status"], output["ratio"], output["warning"]), ("no_data", None, None))
        self.assertEqual(report.rule_self_check(self.results(0, 0))["status"], "no_data")


class WantToTryTests(unittest.TestCase):
    def test_four_combinations(self):
        table = table_of(["a"], ["b"], ["c"], ["d"])
        verdicts = {"m-001": verdict(want=True), "m-002": verdict(want=False),
                    "m-003": verdict(scored=False, want=True), "m-004": verdict(scored=False, want=False)}
        before = report.main_scores({"units": {"a/rep-1": list(verdicts)}}, verdicts)
        output = report.want_summary(table, verdicts)
        self.assertEqual((output["wanted"], output["total"], output["ratio"]), (2, 4, Decimal("0.5")))
        self.assertEqual(output["scored_not_wanted"], [{"mechanism_id": "m-002", "label": "机制2"}])
        flipped = {key: {**row, "want_to_try": not row["want_to_try"]} for key, row in verdicts.items()}
        self.assertEqual(report.main_scores({"units": {"a/rep-1": list(flipped)}}, flipped), before)

    def test_no_verdicts(self):
        self.assertEqual(report.want_summary(None, {})["status"], "no_data")


class ComplexityConfigTests(unittest.TestCase):
    def test_digest_without_complexity_is_frozen(self):
        # Computed at the commit before complexity existed, so recorded units can still resume.
        built = validate_config({"name": "cfg_digest", "default_model": "m", "agents": 2,
                                 "methods": {"a": "b"}, "method_mode": "same"})
        self.assertEqual(digest(built), "aed5117d1e0fbbad7e42b6908cf4618aa0e69926fc21cd1e39eb2986f26f3efa")
        self.assertNotIn("complexity", built)

    def test_written_complexity_is_kept_and_changes_identity(self):
        base = {"name": "cfg_digest", "default_model": "m"}
        self.assertEqual(validate_config({**base, "complexity": 2})["complexity"], 2)
        self.assertNotEqual(digest(validate_config({**base, "complexity": 2})), digest(validate_config({**base, "complexity": 3})))

    def test_invalid_complexity_and_other_unknown_fields_refused(self):
        for value in (0, -1, True, "2", 1.5, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config({"name": "x", "default_model": "m", "complexity": value})
        with self.assertRaisesRegex(ValueError, "Unknown config fields"):
            validate_config({"name": "x", "default_model": "m", "simplicity": 1})


if __name__ == "__main__":
    unittest.main()
