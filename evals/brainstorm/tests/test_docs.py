"""The READMEs must agree with the result files and with each other.

These tests read documents and result summaries only. They catch a number changed in one place and not
in the others, two columns swapped, a broken relative link, and an English and a Chinese page that drifted
apart. A table cell is found by the label of its row and its position, so the column matters.
"""

import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "evals" / "brainstorm" / "results"
SKILL = ROOT / "skills" / "brainstorm"
PAGES = {"en": SKILL / "README.md", "zh": SKILL / "README.zh.md"}
ROOT_PAGES = {"en": ROOT / "README.md", "zh": ROOT / "README.zh.md"}

# Row labels, English then Chinese. Bold marks are ignored.
ROWS = {
    "score": ("Score, two repetitions", "分数，两次重复"),
    "scored_mechanisms": ("Mechanisms per repetition", "每次重复的机制数"),
    "scored_ideas": ("Ideas per repetition", "每次重复的点子数"),
    "scored_new": ('"I had not thought of this", of 17', "17 个里「之前没想到」"),
    "scored_care": ('"I care about this", of 17', "17 个里「我在乎」"),
    "plain": ("Plain asking: calls / ideas / mechanisms", "反复直接问：调用 / 点子 / 机制"),
    "skill": ("Skill: calls / ideas", "技能：调用 / 点子"),
    "skill_only": ("Skill-only mechanisms by the grouper", "分组者口径的技能独有机制"),
    "left": ("Skill-only mechanisms left after the judge", "评委去重后剩下的技能独有机制"),
    "lib_calls": ("Idea calls", "出点子调用"),
    "lib_ideas": ("Skill ideas", "技能点子"),
    "lib_skill_only": ("Skill-only mechanisms by the grouper, two groupings", "分组者口径的技能独有机制，两次分组"),
    "lib_left": ("Left after the judge removed repeats, four passes", "评委去掉重复后剩下的，四遍"),
}


def result(name):
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def numbers(text):
    """Whole numbers in a text. The parts of a decimal such as 7.03 are left out; "25." at a sentence end counts."""
    return [int(value) for value in re.findall(r"(?<!\d)(?<!\d\.)\d+(?!\.?\d)", text)]


def tables(path):
    """Table rows of a page as lists of cells, without the separator rows."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("|") and not re.fullmatch(r"[|:\- ]+", line):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def cells(language, row):
    """Numbers of each value cell of the one row with this label, column by column."""
    label = ROWS[row][0 if language == "en" else 1]
    found = [cells for cells in tables(PAGES[language]) if cells[0].strip("*") == label]
    if len(found) != 1:
        raise AssertionError(f"{language}: expected one row labelled {label!r}, found {len(found)}")
    return [numbers(cell) for cell in found[0][1:]]


def line_with(path, *values):
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if all(value in numbers(line) for value in values)]


class SkillReadmeNumbersTests(unittest.TestCase):
    def both(self, row, expected):
        for language in PAGES:
            self.assertEqual(cells(language, row), expected, f"{language}: row {row}")

    def test_human_score_table_matches_its_result_file(self):
        data = result("2026-11-08-human-scored-cli.json")
        units, scores, answers = data["units"], data["score"]["units"], data["per_question"]
        names = (("direct/rep-1", "direct/rep-2"), ("full/rep-1", "full/rep-2"))
        self.both("score", [[scores[name] for name in side] for side in names])
        self.both("scored_mechanisms", [[units[name]["mechanisms"] for name in side] for side in names])
        self.both("scored_ideas", [[units[name]["ideas"] for name in side] for side in names])
        self.both("scored_new", [[answers[name]["not_thought_of"] for name in side] for side in names])
        self.both("scored_care", [[answers[name]["care"] for name in side] for side in names])
        ratio = round(data["cost"]["ratio_full_to_direct"])
        per_repetition = [round(value) for value in data["cost"]["ratio_per_repetition"]]
        for language, path in PAGES.items():
            self.assertTrue(line_with(path, ratio, *per_repetition), f"{language}: cost ratio {ratio} {per_repetition}")

    def test_equal_call_table_matches_its_result_files(self):
        first = result("2026-11-12-equal-calls-cli.json")
        second = result("2026-11-13-equal-calls-side-income.json")["compare"]
        current = result("2026-11-14-methods-default-vs-library-side-income.json")
        audit = current["audit"]["by_grouping"]
        left = [audit[name][key] for name in ("default-opus-1", "default-opus-2") for key in ("left_pass1", "left_pass2")]
        self.both("plain", [[side["control"][key] for key in ("idea_calls", "ideas", "mechanisms")]
                            for side in (first, second)])
        self.both("skill", [[side["skill"][key] for key in ("idea_calls", "ideas")] for side in (first, second)])
        self.both("skill_only", [[first["same_grouper"]["skill_only"]], current["default_opus_groupings"]["skill_only"]])
        estimate = first["audit_fixed_rule"]["repeats"]["estimated_new"]
        self.both("left", [[estimate], left])
        calls = second["skill"]["calls_by_kind"]
        beyond = (first["skill"]["beyond_its_own_baseline"], second["skill"]["beyond_its_own_baseline"])
        early = [next(row["mechanisms_so_far"] for row in side["control"]["curve"] if row["calls_so_far"] == 9)
                 for side in (first, second)]
        totals = [side["control"]["mechanisms"] for side in (first, second)]
        for language, path in PAGES.items():
            self.assertTrue(line_with(path, min(left), max(left)), f"{language}: range {min(left)} to {max(left)}")
            self.assertTrue(line_with(path, calls["idea"] + calls["rewrite"], calls["grouping"],
                                      calls["idea"] + calls["rewrite"] + calls["grouping"]), f"{language}: calls of one run")
            self.assertTrue(line_with(path, *beyond), f"{language}: beyond the baseline {beyond}")
            self.assertTrue(line_with(path, early[0], totals[0], early[1], totals[1]), f"{language}: plain asking curve")

    def test_method_library_table_matches_its_result_files(self):
        data = result("2026-11-14-methods-default-vs-library-side-income.json")
        audit = data["audit"]["by_grouping"]
        default = [audit[name][key] for name in ("default-opus-1", "default-opus-2") for key in ("left_pass1", "left_pass2")]
        library = [audit[name][key] for name in ("library-opus-1", "library-opus-2") for key in ("left_pass1", "left_pass2")]
        self.both("lib_left", [default, library])
        self.both("lib_skill_only", [data["default_opus_groupings"]["skill_only"],
                                     [row["skill_only"] for row in data["library_opus_groupings"]["runs"]]])
        default_calls = result("2026-11-13-equal-calls-side-income.json")["compare"]["skill"]["calls_by_kind"]
        library_calls = result("2026-11-13-method-library-side-income.json")["skill_calls_by_kind"]
        self.both("lib_calls", [[default_calls["idea"] + default_calls["rewrite"]],
                                [library_calls["idea"] + library_calls["rewrite"]]])
        self.both("lib_ideas", [[result("2026-11-13-equal-calls-side-income.json")["compare"]["skill"]["ideas"]],
                                [data["library_opus_groupings"]["ideas"]["total"]]])
        one = audit["library-opus-1"]
        for language, path in PAGES.items():
            self.assertTrue(line_with(path, one["repeats_pass1"], one["repeats_pass2"], one["skill_only"]),
                            f"{language}: the two judge passes over one library grouping")

    def test_grouper_repeat_numbers_match_their_result_file(self):
        data = result("2026-11-14-grouper-repeat-side-income.json")
        sonnet = [row["skill_only"] for row in data["sonnet_groupings"]["runs"]]
        opus = [row["skill_only"] for row in data["opus_groupings"]["runs"]]
        for language, path in PAGES.items():
            self.assertTrue(line_with(path, *sonnet), f"{language}: Sonnet groupings {sonnet}")
            self.assertTrue(line_with(path, *opus, 96), f"{language}: Opus groupings {opus}")

    def test_english_and_chinese_pages_hold_the_same_numbers_cell_by_cell(self):
        english, chinese = tables(PAGES["en"]), tables(PAGES["zh"])
        self.assertEqual(len(english), len(chinese))
        for left, right in zip(english, chinese):
            self.assertEqual(len(left), len(right), f"rows differ in width:\n{left}\n{right}")
            for a, b in zip(left, right):
                # Word order differs between the languages, so one cell is compared as a bag of numbers.
                self.assertEqual(sorted(numbers(a)), sorted(numbers(b)), f"cells differ:\n{a}\n{b}")

    def test_root_readmes_quote_the_current_range(self):
        audit = result("2026-11-14-methods-default-vs-library-side-income.json")["audit"]["by_grouping"]
        left = [audit[name][key] for name in ("default-opus-1", "default-opus-2") for key in ("left_pass1", "left_pass2")]
        for language, path in ROOT_PAGES.items():
            self.assertTrue(line_with(path, min(left), max(left)), f"{language}: root README range")

    def test_every_result_file_is_described(self):
        index = (RESULTS / "README.md").read_text(encoding="utf-8")
        for path in sorted(RESULTS.glob("*.json")):
            self.assertIn(path.name, index)
            for language, page in PAGES.items():
                self.assertIn(path.name, page.read_text(encoding="utf-8"), language)


class LinkTests(unittest.TestCase):
    def test_relative_links_resolve(self):
        pages = [path for path in ROOT.rglob("*.md")
                 if ".git" not in path.parts and "runs" not in path.parts and "node_modules" not in path.parts]
        self.assertGreater(len(pages), 8)
        dead = []
        for page in pages:
            for target in re.findall(r"\]\(([^)\s]+)\)", page.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                if not (page.parent / target.split("#")[0]).exists():
                    dead.append(f"{page.relative_to(ROOT)} -> {target}")
        self.assertEqual(dead, [])


if __name__ == "__main__":
    unittest.main()
