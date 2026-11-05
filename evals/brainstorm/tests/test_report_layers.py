"""The report a person reads: conclusion first, then the stage table, then folded detail, in Markdown and HTML."""

from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
import re
import tempfile
import unittest

from evals.brainstorm import e2e, report
from evals.brainstorm.config import validate_config
from evals.brainstorm.tests.test_report import NEW, OLD, decide, guard, row
from evals.brainstorm.record import read_file


DETAIL = "明细（需要查原因时再看）"
INTERNAL = re.compile(r"prefer_simpler|indeterminate|matched|complexity|第 \d+ 行|\bm-\d{3}|\bi-[0-9a-f]{12}|level_|`accept`|`reject`")


def checked(count):
    return guard(constraint={"status": "checked", "violations": count})


def plain_config(name="cfg_keep", **options):
    return validate_config({"name": name, "default_model": "gen_alpha", "levels": ["round1", "round2", "pressure1"],
                            "complexity": 1, "agents": 2, "method_mode": "different",
                            "methods": {"angle_east": "x", "angle_north": "y"}, "materials_mode": "none", **options})


def stage(stage_id, label, kind, value, *, present=4, numerator=0, denominator=5, unjudged=0, suspect=None):
    share = Decimal(numerator) / denominator if denominator else None
    return {"stage": stage_id, "label": label, "kind": kind, "value": value, "present": present, "numerator": numerator,
            "denominator": denominator, "share": share, "unjudged": unjudged,
            "suspect": (numerator == 0 and present > 0 and denominator > 0 and not unjudged) if suspect is None else suspect}


def built(*, stages=None, decision=None, config=None, status="ok", backends=("claude",), comparisons=None, **extra):
    config = config or plain_config()
    other = plain_config("cfg_other")
    decision = decision or {"decision": "prefer_simpler", "preferred": config["name"], "matched_row": 6,
                            "reason": "没有明显变好，也没有明显变坏，选更简单的版本", "known_constraint_violations": [],
                            "known_injection_violations": [], "warnings": [], "blockers": []}
    rows = stages if stages is not None else [stage("level_round1", "级别 round1", "level", "round1", numerator=5)]
    contribution = {"status": status, "reason": None, "stages": rows if status == "ok" else [], "denominator": 5}
    entry = {"contribution": {config["name"]: contribution, other["name"]: {"status": "no_data", "stages": [], "denominator": None}},
             "units": {}, "per_unit": 12, "warnings": []}
    result = {"decision": decision, "policy": {"baseline": config["name"], "candidate": other["name"],
                                               "contribution_floor": Decimal(0)},
              "configs": [config, other], "exams": {"exam_a": entry}, "costs": {"backends": list(backends)},
              "unrevealed_sheets": 0, "self_check": {"warning": None}, "receipts": {"ok": True}, "replacements": [],
              "drop_comparisons": comparisons or {}, **extra}
    result["stage_table"] = report.stage_rows(result)
    return result


def lines_of(result):
    return report.summary_lines(result)


class SummaryTests(unittest.TestCase):
    def test_never_more_than_five_lines_and_answers_three_questions(self):
        result = built(backends=("fake",), unrevealed_sheets=2, self_check={"warning": "规则可能挡不住胡话"},
                       replacements=[{"exam_id": "exam_a", "config": "cfg_keep", "failed_rep": 1, "new_rep": 4,
                                      "stage": "recording", "failed_usd": Decimal("0.02"), "new_usd": Decimal("0.09"),
                                      "status": "complete"}])
        lines = lines_of(result)
        self.assertLessEqual(len(lines), 5)
        text = "\n".join(lines)
        for label in ("结论：", "下一步：", "能信多少："):
            self.assertIn(label, text)
        self.assertTrue(all("\n" not in line for line in lines))

    def test_replacement_line_names_the_unit_and_the_money(self):
        row = {"exam_id": "exam_a", "config": "cfg_keep", "failed_rep": 1, "new_rep": 4, "stage": "recording", "cause": "paid_error",
               "failed_usd": Decimal("0.02"), "new_usd": Decimal("0.09"), "status": "complete", "ungrouped_ideas": 0, "reason": None}
        line = lines_of(built(replacements=[row]))[-1]
        self.assertIn("第 1 次重复", line)
        for needle in ("exam_a", "`cfg_keep`", "第 4 次", "0.02 美元", "0.09 美元"):
            self.assertIn(needle, line)
        self.assertIn("不能再替换", lines_of(built(replacements=[{**row, "status": "failed"}]))[-1])

    def test_no_internal_words_for_any_decision(self):
        cases = [
            ("accept", "cfg_other", 4, "1 / 1 道可判断题的均值差超过噪声线"), ("reject", "cfg_keep", 5, "1 / 1 道可判断题的均值差低于负噪声线"),
            ("reject", "cfg_keep", 2, "注入违规非零，共 2 次"), ("reject", "cfg_keep", 3, "成本超过基线的 1.5 倍"),
            ("prefer_simpler", "cfg_keep", 6, "选更简单的版本"), ("prefer_simpler", None, 6, "缺 complexity，选不出版本"),
            ("indeterminate", None, 1, "e1 缺答卷：没有比较卷")]
        for decision, preferred, matched, reason in cases:
            with self.subTest(decision=decision, row=matched):
                text = "\n".join(lines_of(built(decision={
                    "decision": decision, "preferred": preferred, "matched_row": matched, "reason": reason,
                    "known_constraint_violations": [], "known_injection_violations": [], "warnings": [],
                    "blockers": [reason] if matched == 1 else []})))
                self.assertNotRegex(text, INTERNAL)
                self.assertNotIn("命中", text)

    def test_conclusion_names_the_version_to_keep(self):
        self.assertIn("留更简单的 `cfg_keep`", lines_of(built())[0])

    def test_indeterminate_says_why_and_how_many_more(self):
        lines = lines_of(built(decision={"decision": "indeterminate", "preferred": None, "matched_row": 1, "reason": "a；b",
                                         "known_constraint_violations": [], "known_injection_violations": [], "warnings": [],
                                         "blockers": ["第一条原因", "第二条原因", "第三条原因"]}))
        self.assertIn("第一条原因", lines[0])
        self.assertNotIn("第二条原因", lines[0])
        self.assertIn("还有 2 条", lines[0])

    def test_next_step_uses_plain_words_and_the_config_name(self):
        rows = [stage("level_round1", "级别 round1", "level", "round1"), stage("level_pressure1", "级别 pressure1", "level", "pressure1"),
                stage("method_angle_east", "方法 angle_east", "method", "angle_east")]
        line = lines_of(built(stages=rows))[1]
        self.assertIn("第一轮", line)
        self.assertIn("压力一级", line)
        self.assertIn("方法 angle_east", line)
        self.assertIn("`cfg_keep__drop_level_pressure1`", line)
        self.assertNotIn("round1", line.replace("cfg_keep__drop_level_round1", ""))
        self.assertIn("只是相关", line)
        self.assertIn("跑对比", line)

    def test_next_step_lists_three_at_most_with_the_most_spent_first(self):
        rows = [stage(f"method_m{index}", f"方法 m{index}", "method", f"m{index}", present=index + 1) for index in range(5)]
        methods = {f"m{index}": "x" for index in range(5)}
        line = lines_of(built(stages=rows, config=plain_config(methods=methods)))[1]
        self.assertEqual(re.findall(r"方法 (m\d)", line), ["m4", "m3", "m2"])
        self.assertIn("还有 2 个", line)

    def test_next_step_without_suspects_says_so_and_never_writes_zero(self):
        line = lines_of(built(stages=[stage("level_round1", "级别 round1", "level", "round1", numerator=5)]))[1]
        self.assertIn("没有可疑环节", line)
        self.assertNotRegex(line, r"\b0\b")

    def test_next_step_without_data_says_so_and_never_writes_zero(self):
        for result in (built(status="no_data"),
                       built(stages=[stage("level_round1", "级别 round1", "level", "round1", denominator=0)])):
            line = lines_of(result)[1]
            self.assertIn("没有数据", line)
            self.assertNotRegex(line, r"\b0\b")

    def test_next_step_prefers_a_stage_already_shown_removable(self):
        rows = [stage("level_round1", "级别 round1", "level", "round1"), stage("level_round2", "级别 round2", "level", "round2", numerator=5)]
        comparison = {"cfg_keep__drop_level_round2": {"decision": "prefer_simpler", "preferred": "cfg_keep__drop_level_round2",
                                                      "matched_row": 6, "reason": "", "blockers": []}}
        line = lines_of(built(stages=rows, comparisons=comparison))[1]
        self.assertLess(line.index("第二轮"), line.index("第一轮"))
        self.assertIn("可以删", line)

    def test_a_stage_that_cannot_be_removed_is_not_suggested(self):
        rows = [stage("model_gen_alpha", "模型 gen_alpha", "model", "gen_alpha")]
        line = lines_of(built(stages=rows))[1]
        self.assertNotIn("__drop_", line)
        self.assertIn("去不掉", line)

    def test_trust_line_only_names_what_applies(self):
        clean = lines_of(built())[2]
        self.assertNotIn("假后端", clean)
        self.assertNotIn("未还原", clean)
        self.assertIn("没发现", clean)
        loaded = lines_of(built(backends=("fake",), unrevealed_sheets=2, self_check={"warning": "规则可能挡不住胡话"}))[2]
        for needle in ("假后端", "2 张卷子", "规则可能挡不住胡话"):
            self.assertIn(needle, loaded)

    def test_trust_line_names_a_small_sample(self):
        result = built()
        result["exams"]["exam_a"]["per_unit"] = 4
        self.assertIn("每个单元最少只有 4 条", lines_of(result)[2])

    def test_trust_line_names_unfinished_units_and_a_bad_ledger_check(self):
        result = built(receipts={"ok": False})
        result["exams"]["exam_a"]["units"] = {"cfg_keep": {1: {"completion": {"status": "not_evaluated", "reason": "x"}}}}
        line = lines_of(result)[2]
        self.assertIn("1 个单元", line)
        self.assertIn("账本", line)


class StageTableTests(unittest.TestCase):
    def only(self, result, stage_id):
        return next(row for row in result["stage_table"] if row["stage"] == stage_id)

    def test_not_tested_never_reads_as_zero(self):
        row = self.only(built(), "level_round1")
        self.assertEqual(row["removal_result"], "还没测")
        self.assertEqual(row["needed"], "5 / 5")
        self.assertEqual(row["advice"], "留下")

    def test_the_four_pieces_of_advice(self):
        rows = [stage("level_round1", "l", "level", "round1", numerator=5),
                stage("level_round2", "l", "level", "round2", numerator=0),
                stage("level_pressure1", "l", "level", "pressure1", numerator=5)]
        comparison = {"cfg_keep__drop_level_pressure1": {"decision": "prefer_simpler", "preferred": "cfg_keep__drop_level_pressure1",
                                                         "matched_row": 6, "reason": "", "blockers": []}}
        result = built(stages=rows, comparisons=comparison)
        self.assertEqual(self.only(result, "level_round1")["advice"], "留下")
        self.assertEqual(self.only(result, "level_round2")["advice"], "试着去掉")
        self.assertEqual(self.only(result, "level_pressure1")["advice"], "可以删")
        self.assertEqual(self.only(result, "level_pressure1")["removal_result"], "去掉后没明显变化")
        empty = built(status="no_data")
        self.assertTrue(all(row["advice"] == "没数据" and row["needed"] == "无数据" and row["removal_result"] in ("还没测", "去不掉：这个配置只有一个模型，不能去掉")
                            for row in empty["stage_table"]))
        self.assertEqual({row["advice"] for result in (result, empty) for row in result["stage_table"]},
                         {"留下", "试着去掉", "可以删", "没数据"})

    def test_comparison_outcomes_map_to_advice(self):
        cases = [("accept", "cfg_keep__drop_level_round1", 4, "去掉后变好", "可以删"),
                 ("prefer_simpler", "cfg_keep__drop_level_round1", 6, "去掉后没明显变化", "可以删"),
                 ("reject", "cfg_keep", 5, "去掉后变差", "留下"), ("reject", "cfg_keep", 3, "去掉后硬约束或成本变差", "留下"),
                 ("reject", "cfg_keep", 2, "对比里有注入违规", "留下")]
        for decision, preferred, matched, words, advice in cases:
            with self.subTest(decision=decision, row=matched):
                result = built(comparisons={"cfg_keep__drop_level_round1": {"decision": decision, "preferred": preferred,
                                                                            "matched_row": matched, "reason": "", "blockers": []}})
                row = self.only(result, "level_round1")
                self.assertEqual((row["removal_result"], row["advice"]), (words, advice))

    def test_undecided_comparison_says_so_and_keeps_the_evidence_advice(self):
        result = built(stages=[stage("level_round1", "l", "level", "round1", numerator=0)],
                       comparisons={"cfg_keep__drop_level_round1": {"decision": "indeterminate", "preferred": None, "matched_row": 1,
                                                                    "reason": "x", "blockers": ["缺答卷"]}})
        row = self.only(result, "level_round1")
        self.assertIn("判断不了", row["removal_result"])
        self.assertIn("缺答卷", row["removal_result"])
        self.assertEqual(row["advice"], "试着去掉")

    def test_a_stage_only_waiting_for_answers_has_no_data_yet(self):
        row = self.only(built(stages=[stage("level_round1", "l", "level", "round1", numerator=0, unjudged=3)]), "level_round1")
        self.assertEqual(row["advice"], "没数据")
        self.assertIn("3 条没判", row["needed"])

    def test_counts_add_up_across_exams(self):
        result = built(stages=[stage("level_round1", "l", "level", "round1", numerator=2, denominator=5)])
        twin = dict(result["exams"]["exam_a"])
        twin["contribution"] = {"cfg_keep": {"status": "ok", "stages": [stage("level_round1", "l", "level", "round1", numerator=1, denominator=4)],
                                             "denominator": 4}, "cfg_other": {"status": "no_data", "stages": [], "denominator": None}}
        result["exams"]["exam_b"] = twin
        self.assertEqual(report.stage_rows(result)[0]["needed"], "3 / 9")

    def test_stage_only_the_other_version_has_gets_a_row_with_its_owner(self):
        result = built()
        other = plain_config("cfg_other", return_collisions=True)
        result["configs"][1] = other
        result["exams"]["exam_a"]["contribution"]["cfg_other"] = {"status": "ok", "denominator": 5, "stages": [
            stage("level_round1", "l", "level", "round1", numerator=1), stage("return", "撞车退回", "return", None, numerator=0)]}
        rows = report.stage_rows(result)
        self.assertEqual(len([row for row in rows if row["stage"] == "level_round1"]), 1)
        back = next(row for row in rows if row["stage"] == "return")
        self.assertEqual(back["config"], "cfg_other")
        self.assertIn("`cfg_other` 才有", back["name"])

    def test_switch_without_lineage_has_no_share_but_can_still_be_compared(self):
        result = built(config=plain_config(three_step=True))
        row = self.only(result, "switch_three_step")
        self.assertEqual((row["name"], row["advice"], row["removal_result"]), ("三步法", "没数据", "还没测"))
        self.assertIn("没记录", row["needed"])

    def test_advice_follows_the_sum_the_row_shows(self):
        result = built(stages=[stage("level_round1", "l", "level", "round1", numerator=0, denominator=5)])
        twin = dict(result["exams"]["exam_a"])
        twin["contribution"] = {"cfg_keep": {"status": "ok", "denominator": 5, "stages": [stage("level_round1", "l", "level", "round1", numerator=2)]},
                                "cfg_other": {"status": "no_data", "stages": [], "denominator": None}}
        result["exams"]["exam_b"] = twin
        row = report.stage_rows(result)[0]
        self.assertEqual((row["needed"], row["advice"]), ("2 / 10", "留下"))

    def test_names_are_plain(self):
        rows = [stage(f"level_{value}", "l", "level", value, numerator=5) for value in ("round1", "round2", "pressure1")]
        names = [row["name"] for row in built(stages=rows)["stage_table"]]
        self.assertEqual(names[:3], ["第一轮", "第二轮", "压力一级"])


class ConstraintPerExamTests(unittest.TestCase):
    def assertRejected(self, result, exam_id):
        self.assertEqual((result["decision"], result["matched_row"]), ("reject", 3), result["reason"])
        self.assertEqual(result["preferred"], OLD)
        self.assertIn("硬约束", result["reason"])
        self.assertIn(exam_id, result["reason"])

    def test_a_rise_on_one_exam_is_not_hidden_by_a_drop_on_another(self):
        rows = [row("e1", new=(9, 9, 9), old_guard=checked(0), new_guard=checked(1)),
                row("e2", new=(9, 9, 9), old_guard=checked(1), new_guard=checked(0))]
        result = decide(rows)
        self.assertRejected(result, "e1")
        self.assertNotIn("e2", result["reason"])

    def test_every_triggering_exam_is_named(self):
        rows = [row("e1", old_guard=checked(0), new_guard=checked(1)), row("e2", old_guard=checked(0), new_guard=checked(2))]
        result = decide(rows)
        self.assertRejected(result, "e1")
        self.assertIn("e2", result["reason"])
        self.assertEqual([entry["exam_id"] for entry in result["constraint_rises"]], ["e1", "e2"])

    def test_means_are_compared_per_exam_not_single_repetitions(self):
        low, high = row("e1", old_guard=checked(0), new_guard=checked(0)), None
        low["guards"][NEW] = [checked(1), checked(0), checked(0)]
        low["guards"][OLD] = [checked(0), checked(1), checked(0)]
        self.assertEqual(decide([low])["matched_row"], 6)
        high = row("e1", old_guard=checked(0), new_guard=checked(0))
        high["guards"][NEW] = [checked(1), checked(0), checked(0)]
        self.assertRejected(decide([high]), "e1")

    def test_equal_or_lower_on_every_exam_passes(self):
        rows = [row("e1", old_guard=checked(1), new_guard=checked(1)), row("e2", old_guard=checked(2), new_guard=checked(0))]
        self.assertEqual(decide(rows)["matched_row"], 6)

    def test_only_decision_exams_count(self):
        rows = [row("e1"), row("outside", old_guard=checked(0), new_guard=checked(3))]
        self.assertEqual(decide(rows, ids=["e1"])["matched_row"], 6)

    def test_unknown_or_partial_still_stops_at_the_first_row(self):
        for bad in ({"status": "not_evaluated", "violations": None}, {"status": "not_evaluated", "violations": 1}):
            with self.subTest(bad=bad):
                rows = [row("e1", old_guard=checked(0), new_guard=guard(constraint=bad)),
                        row("e2", old_guard=checked(1), new_guard=checked(0))]
                self.assertEqual(decide(rows)["matched_row"], 1)

    def test_not_applicable_never_counts_as_a_rise(self):
        self.assertEqual(decide([row("e1")])["matched_row"], 6)

    def test_cost_is_still_compared_as_a_total(self):
        rows = [row("e1", old_cost=1, new_cost=3), row("e2", old_cost=3, new_cost=1)]
        result = decide(rows)
        self.assertEqual(result["matched_row"], 6)

    def test_totals_field_stays_for_readers_of_the_json(self):
        result = decide([row("e1", old_guard=checked(0), new_guard=checked(0))])
        self.assertEqual(result["totals"]["constraint_violations"], {OLD: 0, NEW: 0})
        self.assertEqual(result["constraint_rises"], [])

    def test_guard_section_says_which_exam_and_that_it_is_per_exam(self):
        result = decide([row("e1", old_guard=checked(0), new_guard=checked(1))])
        lines = report.constraint_lines(result)
        self.assertTrue(any("e1" in line and "硬约束" in line for line in lines), lines)
        self.assertTrue(any("按单题" in line for line in lines), lines)


class HtmlSafety(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs = [], []
        self.open_details = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)
        if tag == "details":
            self.open_details.append(dict(attrs))


class RenderedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.root = Path(temp.name).resolve() / "run"
        e2e_summary = e2e.run_e2e(cls.root, exam_ids=("open_sample",), reps=1)
        cls.summary = e2e_summary
        cls.markdown = (cls.root / "report.md").read_text(encoding="utf-8")
        cls.html = (cls.root / "report.html").read_text(encoding="utf-8")
        cls.json = read_file(cls.root / "report.json")

    def test_markdown_opens_with_the_three_layers_in_order(self):
        self.assertLess(self.markdown.index("## 先看这里"), self.markdown.index("## 环节表"))
        self.assertLess(self.markdown.index("## 环节表"), self.markdown.index(f"## {DETAIL}"))
        for number, title in enumerate(table_tests_titles(), 1):
            self.assertLess(self.markdown.index(f"## {DETAIL}"), self.markdown.index(f"<summary>{number}. {title}"), title)

    def test_summary_section_holds_at_most_five_lines_and_no_internal_words(self):
        block = self.markdown.split("## 先看这里\n", 1)[1].split("\n## ", 1)[0]
        lines = [line for line in block.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 5)
        self.assertNotRegex(re.sub(r"`[^`]+`", "", "\n".join(lines)), INTERNAL)
        self.assertTrue(lines[0].startswith("- **结论：**"), lines[0])

    def test_stage_table_has_the_four_columns_and_only_the_four_answers(self):
        block = self.markdown.split("## 环节表\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn("| 环节 | 计分点子里有多少必须经过它 | 去掉后对比结果 | 建议 |", block)
        rows = [line for line in block.splitlines() if line.startswith("| ")][1:]
        self.assertTrue(rows)
        advice = {line.rstrip(" |").rsplit("| ", 1)[1] for line in rows}
        self.assertLessEqual(advice, {"留下", "试着去掉", "可以删", "没数据"})
        self.assertTrue(all("还没测" in line or "去不掉" in line for line in rows))
        self.assertNotIn("| 0 |", block)

    def test_every_detail_section_is_folded_in_both_files(self):
        for text in (self.markdown, self.html):
            for number, title in enumerate(table_tests_titles(), 1):
                self.assertIn(f"<summary>{number}. {title}</summary>", text)
            self.assertEqual(text.count("<details"), len(table_tests_titles()))
            self.assertEqual(text.count("</details>"), len(table_tests_titles()))
        parsed = HtmlSafety()
        parsed.feed(self.html)
        self.assertTrue(all("open" not in attrs for attrs in parsed.open_details))

    def test_html_and_markdown_carry_the_same_words(self):
        def words(text):
            text = re.sub(r"<style>.*?</style>", "", text, flags=re.S)
            text = re.sub(r"<title>.*?</title>", "", text)
            text = re.sub(r"<[^>]+>", " ", text)
            text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"').replace("&#x27;", "'")
            return re.sub(r"[\s|*`#:\-]+", "", text)
        self.assertEqual(words(self.html), words(self.markdown))

    def test_html_is_one_file_with_nothing_external(self):
        parsed = HtmlSafety()
        parsed.feed(self.html)
        self.assertFalse({"script", "link", "img", "iframe", "object", "embed"} & set(parsed.tags))
        self.assertFalse([name for name, _ in parsed.attrs if name in ("src", "href") or name.startswith("on")])
        self.assertNotRegex(self.html.split("</style>")[0], r"https?://|@import|url\(")
        self.assertIn("default-src 'none'", self.html)

    def test_html_reads_in_light_and_dark(self):
        self.assertIn("color-scheme: light dark", self.html)
        self.assertIn("prefers-color-scheme: dark", self.html)
        self.assertIn('<meta name="viewport"', self.html)

    def test_report_json_keeps_old_fields_and_gains_the_new_ones(self):
        for field in ("decision", "policy", "exams", "configs", "contribution_note", "sample_note", "leave_one", "failures",
                      "self_check", "costs", "receipts", "calibration", "replay", "unrevealed_sheets", "warnings", "schema_version"):
            self.assertIn(field, self.json)
        for field in ("summary", "stage_table", "replacements", "drop_comparisons"):
            self.assertIn(field, self.json)
        self.assertEqual(self.json["summary"], report.summary_lines(self.json))


class EscapeTests(unittest.TestCase):
    HOSTILE = ['<script>alert(1)</script>', '"><img src=x onerror=alert(2)>', "</details><b>out</b>", "a | b", "`<i>code</i>`", "**<u>bold</u>**"]

    def page(self, lines):
        return report.render_html(lines["title"], lines["summary"], lines["table"], lines["details"])

    def test_every_position_is_escaped(self):
        for text in self.HOSTILE:
            with self.subTest(text=text):
                page = self.page({"title": text, "summary": [f"结论：{text}"], "table": [f"说明 {text}", "",
                                  *report.md_table(["环节", "建议"], [[text, "留下"]])],
                                  "details": [(text, [text, f"- {text}", f"  - {text}", f"**{text}**", *report.md_table(["a"], [[text]])])]})
                parsed = HtmlSafety()
                parsed.feed(page)
                self.assertFalse({"script", "img", "b", "i", "u"} & set(parsed.tags), page)
                self.assertFalse([name for name, _ in parsed.attrs if name.startswith("on") or name == "src"])
                self.assertEqual(page.count("<details"), 1)
                self.assertEqual(page.count("</details>"), 1)

    def test_pipe_in_a_cell_stays_in_its_cell(self):
        page = self.page({"title": "t", "summary": [], "table": report.md_table(["a", "b"], [["x | y", "z"]]), "details": []})
        self.assertEqual(page.count("<td"), 2)
        self.assertIn("x | y", page)

    def test_markdown_detail_cannot_close_its_own_fold(self):
        text = report.fold_markdown("标题", ["</details>", "<script>alert(1)</script>"])
        self.assertEqual(text.count("</details>"), 1)
        self.assertNotIn("<script>", "\n".join(text.splitlines()))

    def test_hostile_idea_text_in_a_real_report_stays_escaped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "run"
            e2e.run_e2e(root, exam_ids=("open_sample",), reps=1)
            hostile = "<script>alert('idea')</script>"
            built_report = read_file(root / "report.json")
            built_report["exams"]["open_sample"]["expansion"][0]["members"][0]["text"] = hostile
            page = report.render_report_html(built_report)
            self.assertNotIn(hostile, page)
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn(hostile, report.render_markdown(built_report))


def table_tests_titles():
    return ("判定", "主分和噪声线", "护栏和成本", "贡献度", "留一对照", "失败账本", "计分规则自检", "想试比例和计了分但不想试",
            "机器指标和两种比较口径", "费用", "条数核对", "校准", "被合并的说法", "单元替换")


class DropComparisonFlowTests(unittest.TestCase):
    """A leave-one config that really ran fills the table through the same sheets and report as any comparison."""

    def test_recorded_removal_fills_its_row(self):
        from evals.brainstorm.tests.test_replace_unit import Flow
        flow = Flow("run_unit")
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        parent = flow.plain
        changes, reason = report.drop_changes(parent, "level", "pressure1")
        self.assertIsNone(reason)
        dropped = validate_config({**parent, **changes, "name": f"{parent['name']}__drop_level_pressure1"})
        for config in (parent, flow.returned, dropped):
            for rep in (1, 2, 3):
                flow.run_unit(config, rep)
                report.judge_unit(flow.root, flow.exam, config["name"], rep, backend=flow.judging, ledger=flow.ledger, model="clf_alpha")
        flow.compare_sheet("main_pair", [parent["name"], flow.returned["name"]])
        flow.compare_sheet("removal_pair", [parent["name"], dropped["name"]], seed=8)
        built_report = report.build_report(flow.root, [flow.exam], [parent, flow.returned, dropped], baseline=parent["name"],
                                           candidate=flow.returned["name"], decision_exam_ids=[flow.exam["id"]],
                                           ledger_path=flow.ledger.path)
        self.assertIn(dropped["name"], built_report["drop_comparisons"])
        row = next(item for item in built_report["stage_table"] if item["stage"] == "level_pressure1")
        self.assertNotEqual(row["removal_result"], "还没测")
        self.assertIn(row["advice"], ("留下", "可以删", "试着去掉", "没数据"))
        other = next(item for item in built_report["stage_table"] if item["stage"] == "level_round2")
        self.assertEqual(other["removal_result"], "还没测")


if __name__ == "__main__":
    unittest.main()
