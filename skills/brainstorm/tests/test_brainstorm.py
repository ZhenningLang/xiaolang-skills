"""Tests for scripts/brainstorm.py. Run: python3 -m unittest discover -s skills/brainstorm/tests < /dev/null"""

import contextlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
sys.path.insert(0, str(HERE.parent / "scripts"))

import brainstorm as bs  # noqa: E402


def data_block(prompt, header):
    """Read the JSON inside the data boundary that follows a header line, as a sub-agent would."""
    lines = prompt.split("\n")
    start = lines.index(header)
    boundary = lines[start + 2]
    end = lines.index(boundary, start + 3)
    return json.loads("\n".join(lines[start + 3:end]))


def output_path(prompt):
    lines = prompt.split("\n")
    return Path(lines[lines.index(next(l for l in lines if l.startswith("Deliver that JSON"))) + 1])


class FakeHost:
    """Plays the host and its sub-agents with hand-written fixtures; never calls a model."""

    def __init__(self, run_dir, broken=None, grouping=None, pressure="go"):
        self.run_dir = run_dir
        self.broken = broken or {}      # task id -> "missing" | fixture file name
        self.grouping = grouping or {}  # grouping task id -> "missing" | fixture file name
        self.pressure = pressure
        self.actions = []

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bs.main(list(args))
        if code != 0:
            raise AssertionError(f"command failed: {args}")
        return json.loads(out.getvalue())

    def generate(self, task):
        prompt = Path(task["prompt"]).read_text(encoding="utf-8")
        target = output_path(prompt)
        self_check = target == Path(task["output"])
        assert self_check, "prompt must name the task's own output file"
        level = re.search(r"^Generation level: (\w+)\.$", prompt, re.M).group(1)
        agent = re.match(r"b\d+-a(\d)", task["id"]).group(1)
        rule = self.broken.get(task["id"])
        if rule == "missing":
            return
        if rule:
            target.write_text((FIXTURES / rule).read_text(encoding="utf-8"), encoding="utf-8")
            return
        returned = re.search(r"The originals are numbered 1 to (\d+)", prompt)
        name = "rewrite.json" if returned else f"{level}.json"
        payload = json.loads((FIXTURES / name).read_text(encoding="utf-8").replace("{agent}", agent))
        if returned:
            payload["ideas"] = payload["ideas"][:int(returned.group(1))]
            for number, idea in enumerate(payload["ideas"], 1):
                idea["replaces"] = number
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def group(self, task):
        """Group by identical text only: a wiring fixture, not a semantic judge."""
        prompt = Path(task["prompt"]).read_text(encoding="utf-8")
        target = output_path(prompt)
        rule = self.grouping.get(task["id"])
        if rule == "missing":
            return
        if rule:
            target.write_text((FIXTURES / rule).read_text(encoding="utf-8"), encoding="utf-8")
            return
        existing = {row["example"]["text"]: row["mechanism_id"] for row in data_block(prompt, "Existing mechanisms:")}
        groups = {}
        for row in data_block(prompt, "New ideas:"):
            key = existing.get(row["text"], "new:" + str(abs(hash(row["text"]))))
            groups.setdefault(key, {"target": key, "label": row["title"], "idea_ids": []})["idea_ids"].append(row["idea_id"])
        target.write_text(json.dumps({"groups": list(groups.values())}, ensure_ascii=False), encoding="utf-8")

    def run(self, max_steps=200):
        extra = []
        for _ in range(max_steps):
            action = self.cli("next", "--dir", str(self.run_dir), *extra)
            extra = []
            self.actions.append(action)
            if action["action"] == "done":
                return action
            if action["action"] == "confirm_pressure":
                extra = ["--pressure", self.pressure]
                continue
            if action["action"] == "wait":
                extra = ["--give-up"]
                continue
            for task in action["tasks"]:
                (self.group if action["purpose"] == "group" else self.generate)(task)
        raise AssertionError("run did not finish")


class RunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "run"

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, *extra, cards=True):
        args = ["init", "--dir", str(self.dir), "--question-file", str(FIXTURES / "question.txt"),
                "--constraint", "No telemetry", "--constraint", "Two hours a week of maintainer time", "--seed", "7"]
        if cards:
            args += ["--cards", str(FIXTURES / "cards.jsonl")]
        return FakeHost(self.dir).cli(*args, *extra)

    def state(self):
        return json.loads((self.dir / "state.json").read_text(encoding="utf-8"))


class AllocationTests(unittest.TestCase):
    def test_same_seed_reproduces_assignments(self):
        cards = [f"c{i:02d}" for i in range(1, 11)]
        first = bs.assignments_for(cards, 123, 4, "round1")
        self.assertEqual(first, bs.assignments_for(cards, 123, 4, "round1"))
        self.assertNotEqual(first, bs.assignments_for(cards, 124, 4, "round1"))
        self.assertEqual(len({row["method"] for row in first}), 3)
        self.assertTrue(all(len(row["cards"]) == 5 for row in first))

    def test_matches_the_evaluated_harness(self):
        # Recorded by the evaluation harness: seed 20261107, batch 4, round 1, ten cards c01..c10.
        cards = [f"c{i:02d}" for i in range(1, 11)]
        self.assertEqual(bs.assignments_for(cards, 20261107, 4, "round1"), [
            {"agent": 1, "method": "reverse", "cards": ["c01", "c04", "c05", "c08", "c10"]},
            {"agent": 2, "method": "person", "cards": ["c01", "c02", "c04", "c05", "c08"]},
            {"agent": 3, "method": "resources", "cards": ["c01", "c02", "c03", "c04", "c06"]}])

    def test_baseline_has_no_method(self):
        rows = bs.assignments_for(["a", "b", "c", "d"], 1, 1, "baseline")
        self.assertTrue(all(row["method"] is None for row in rows))

    def test_pressure2_avoids_used_cards_when_enough_are_fresh(self):
        cards = [f"c{i:02d}" for i in range(1, 31)]
        used = set(cards[:15])
        for seed in range(20):
            rows = bs.assignments_for(cards, seed, 11, "pressure2", used)
            for row in rows:
                self.assertEqual(len(row["cards"]), 15)
                self.assertFalse(set(row["cards"]) & used, f"seed {seed} reused a card")

    def test_pressure2_spends_scarce_fresh_cards_first(self):
        cards = [f"c{i:02d}" for i in range(1, 11)]
        used = set(cards[:8])
        for row in bs.assignments_for(cards, 5, 11, "pressure2", used):
            self.assertEqual(set(row["fresh_cards"]), {"c09", "c10"})
            self.assertLessEqual({"c09", "c10"}, set(row["cards"]))

    def test_no_cards_gives_empty_assignments(self):
        rows = bs.assignments_for([], 3, 1, "round1")
        self.assertTrue(all(row["cards"] == [] for row in rows))

    def test_a_single_card_reaches_every_agent(self):
        for level in ("baseline", "round1", "pressure2"):
            rows = bs.assignments_for(["c01"], 3, 1, level, {"c01"})
            self.assertTrue(all(row["cards"] == ["c01"] for row in rows), level)

    def test_pressure2_has_no_fresh_cards_once_the_baseline_used_them_all(self):
        # Nine baseline agents each hold half the cards, so in a real run every card is used before pressure 2.
        cards = [f"c{n:02d}" for n in range(1, 11)]
        for row in bs.assignments_for(cards, 5, 11, "pressure2", set(cards)):
            self.assertEqual(row["fresh_cards"], [])
            self.assertEqual(len(row["cards"]), 5)


class ValidationTests(unittest.TestCase):
    def test_parse_ideas_rejects_extra_fields_and_unassigned_cards(self):
        good = {"title": "t", "text": "x", "cards_used": ["c01"], "decomposition": None}
        self.assertEqual(len(bs.parse_ideas(json.dumps({"ideas": [good]}), ["c01"])), 1)
        self.assertEqual(len(bs.parse_ideas("```json\n" + json.dumps({"ideas": [good]}) + "\n```", ["c01"])), 1)
        with self.assertRaises(ValueError):
            bs.parse_ideas(json.dumps({"ideas": [{**good, "score": 3}]}), ["c01"])
        with self.assertRaises(ValueError):
            bs.parse_ideas(json.dumps({"ideas": [good]}), ["c02"])
        with self.assertRaises(ValueError):
            bs.parse_ideas(json.dumps({"ideas": [{**good, "decomposition": "a plain string"}]}), ["c01"])

    def test_partition_keeps_unambiguous_assignments(self):
        ideas = {"i-1": {"title": "one"}, "i-2": {"title": "two"}, "i-3": {"title": "three"}}
        reply = json.dumps({"groups": [{"target": "m-001", "label": "", "idea_ids": ["i-1"]},
                                       {"target": "new:x", "label": "fresh", "idea_ids": ["i-2", "i-3"]},
                                       {"target": "new:y", "label": "dup", "idea_ids": ["i-3"]}]})
        buckets, ungrouped, errors, unreadable = bs.parse_partition(reply, ["i-1", "i-2", "i-3"], ideas, {"m-001"})
        self.assertFalse(unreadable)
        self.assertEqual(ungrouped, ["i-3"])
        self.assertEqual({b["target"]: b["idea_ids"] for b in buckets},
                         {"m-001": ["i-1"], "new:x": ["i-2"], "ungrouped:i-3": ["i-3"]})


class RunTests(RunCase):
    def test_question_is_stored_verbatim(self):
        self.init()
        raw = (FIXTURES / "question.txt").read_text(encoding="utf-8")
        self.assertEqual(self.state()["question"], raw)

    def test_full_run_end_to_end(self):
        self.init()
        host = FakeHost(self.dir)
        done = host.run()
        state = self.state()
        kinds = [a["action"] for a in host.actions]
        self.assertEqual(kinds.count("confirm_pressure"), 1)
        levels = [a.get("level") for a in host.actions if a["action"] == "dispatch" and a["purpose"] == "generate"]
        self.assertEqual(levels, ["baseline", "round1", "round2", "pressure1", "pressure2", "pressure3"])
        self.assertEqual(sum(len(a["tasks"]) for a in host.actions if a.get("level") == "baseline"
                             and a.get("purpose") == "generate"), 9)
        # Round 1 repeats a baseline idea; each agent gets exactly one rewrite, then no further return.
        rewrites = [a for a in host.actions if a.get("purpose") == "rewrite"]
        self.assertEqual([a["level"] for a in rewrites], ["round1"])
        self.assertEqual(len(rewrites[0]["tasks"]), 3)
        returned = [i for i in state["ideas"].values() if i["returned"]]
        self.assertEqual(len(returned), 3)
        self.assertTrue(all(state["ideas"][i["returned_from"]]["level"] == "round1" for i in returned))
        # 3 baseline mechanisms (round 1 collisions merged in) + 6 round1 + 3 rewrites + 4 levels x 6.
        self.assertEqual(done["mechanisms"], 3 + 6 + 3 + 24)
        self.assertEqual(done["beyond_baseline"], done["mechanisms"] - 3)
        self.assertEqual(len(state["mechanisms"]), done["mechanisms"])
        report = Path(done["report"]).read_text(encoding="utf-8")
        headings = re.findall(r"^### (\d+)\. ", report, re.M)
        self.assertEqual(len(headings), done["mechanisms"])
        body = report[report.index("## Beyond the baseline"):]  # the contents list above has its own fold
        self.assertLess(body.index("<details>"), body.index("**Better README quick start**"))
        self.assertEqual(report.count("<details>"), 2)
        self.assertIn("`round1 rewrite`", report)
        self.assertIn("`pressure3`", report)
        self.assertIn("collided with the baseline", report)
        # Round 2 and pressure 3 receive what the design says they receive.
        round2 = next(a for a in host.actions if a.get("level") == "round2" and a["purpose"] == "generate")
        prompt = Path(round2["tasks"][0]["prompt"]).read_text(encoding="utf-8")
        self.assertIn("Supplied decompositions:", prompt)
        earlier = data_block(prompt, bs.PREVIOUS_HEADER)
        avoid = data_block(prompt, "Avoid these already observed mechanisms:")
        self.assertTrue(earlier)
        self.assertTrue(all(set(row) == {"title"} for row in earlier))  # full text is not repeated
        self.assertLessEqual({row["title"] for row in earlier}, {row["title"] for row in avoid})
        self.assertTrue(all(set(row) == {"title", "text"} for row in avoid))
        self.assertIn("Avoid these already observed mechanisms:", prompt)
        p3 = next(a for a in host.actions if a.get("level") == "pressure3" and a["purpose"] == "generate")
        self.assertIn("question these observations", Path(p3["tasks"][0]["prompt"]).read_text(encoding="utf-8"))
        first = Path(host.actions[0]["tasks"][0]["prompt"]).read_text(encoding="utf-8")
        self.assertIn((FIXTURES / "question.txt").read_text(encoding="utf-8"), first)
        self.assertIn("same language as the original question", first)
        self.assertNotIn("Avoid these already observed mechanisms", first)

    def test_broken_agents_are_dropped_and_the_run_continues(self):
        self.init()
        host = FakeHost(self.dir, broken={"b004-a2": "not_json.txt", "b004-a3": "missing", "b001-a1": "bad_shape.json"})
        done = host.run()
        state = self.state()
        reasons = {row["task"]: row["reason"] for row in state["failures"]}
        self.assertEqual(reasons, {"b001-a1": "invalid_output", "b004-a2": "invalid_output", "b004-a3": "missing_output"})
        self.assertFalse(any(i["task"] in reasons for i in state["ideas"].values()))
        self.assertEqual(done["action"], "done")
        report = Path(done["report"]).read_text(encoding="utf-8")
        self.assertIn("## Dropped or partial sub-agent outputs", report)
        summary = {row["level"]: row for row in state["summary"] if "ideas" in row}
        self.assertEqual(summary["round1"]["failed_agents"], 2)

    def test_next_waits_while_answers_are_missing(self):
        self.init()
        host = FakeHost(self.dir)
        first = host.cli("next", "--dir", str(self.dir))
        before = (self.dir / "state.json").read_text(encoding="utf-8")
        waiting = host.cli("next", "--dir", str(self.dir))
        self.assertEqual(waiting["action"], "wait")
        self.assertEqual([t["id"] for t in waiting["missing"]], [t["id"] for t in first["tasks"]])
        self.assertEqual(waiting["missing"][0]["prompt"], first["tasks"][0]["prompt"])
        self.assertEqual((self.dir / "state.json").read_text(encoding="utf-8"), before)  # nothing dropped
        for task in first["tasks"][1:]:
            host.generate(task)
        waiting = host.cli("next", "--dir", str(self.dir))
        self.assertEqual([t["id"] for t in waiting["missing"]], [first["tasks"][0]["id"]])
        host.generate(first["tasks"][0])
        self.assertEqual(host.cli("next", "--dir", str(self.dir))["action"], "dispatch")
        self.assertEqual(self.state()["failures"], [])

    def test_give_up_drops_only_the_missing_answers(self):
        self.init()
        host = FakeHost(self.dir)
        first = host.cli("next", "--dir", str(self.dir))
        for task in first["tasks"][1:]:
            host.generate(task)
        host.cli("next", "--dir", str(self.dir), "--give-up")
        self.assertEqual([(row["task"], row["reason"]) for row in self.state()["failures"]],
                         [(first["tasks"][0]["id"], "missing_output")])

    def test_report_opens_with_one_line_per_mechanism(self):
        self.init("--profile", "economy")
        done = FakeHost(self.dir).run()
        report = Path(done["report"]).read_text(encoding="utf-8")
        contents = report[report.index("## Contents"):report.index("## Beyond the baseline")]
        numbers = [int(n) for n in re.findall(r"^- (\d+)\. ", contents, re.M)]
        self.assertEqual(numbers, list(range(1, done["mechanisms"] + 1)))
        heading = re.search(r"^### 1\. (.*)$", report, re.M).group(1)
        self.assertIn(f"- 1. {heading} ·", contents)
        self.assertLess(contents.index(f"- {done['beyond_baseline']}. "), contents.index("<details>"))
        self.assertLess(contents.index("<details>"), contents.index(f"- {done['beyond_baseline'] + 1}. "))

    def test_unreadable_grouping_is_retried_once_then_kept_ungrouped(self):
        self.init("--profile", "economy")
        host = FakeHost(self.dir, grouping={"g001": "missing", "g002": "not_json.txt"})
        done = host.run()
        state = self.state()
        self.assertEqual([row["reason"] for row in state["failures"][:2]], ["grouping_unreadable", "ungrouped"])
        ungrouped = [row for row in state["mechanisms"] if row["ungrouped"]]
        self.assertEqual(len(ungrouped), 9)  # first baseline batch: 3 agents x 3 ideas, each kept on its own
        self.assertIn("not grouped", Path(done["report"]).read_text(encoding="utf-8"))

    def test_not_grouped_mark_stays_and_says_only_the_first_idea_was_not_grouped(self):
        # The first idea of such a mechanism was never compared with the mechanisms before it, whatever joins later.
        self.init("--profile", "economy")
        host = FakeHost(self.dir, grouping={"g001": "missing", "g002": "not_json.txt"})
        done = host.run()
        state = self.state()
        marked = [row for row in state["mechanisms"] if row["ungrouped"]]
        self.assertEqual(len(marked), 9)
        self.assertTrue(any(len(row["idea_ids"]) > 1 for row in marked))
        report = Path(done["report"]).read_text(encoding="utf-8")
        headings = [line for line in report.splitlines() if line.startswith("### ") and "its first idea was not grouped" in line]
        self.assertEqual(len(headings), 9)

    def test_pressure2_of_a_real_run_has_no_fresh_card_left(self):
        self.init()
        FakeHost(self.dir).run()
        tasks = [task for task in self.state()["tasks"].values()
                 if task["kind"] == "generate" and task["level"] == "pressure2"]
        self.assertEqual(len(tasks), 3)
        for task in tasks:
            self.assertEqual(task["fresh_cards"], [])
            self.assertEqual(len(task["cards"]), len(self.state()["cards"]) // 2)

    def test_done_counts_dropped_agents_apart_from_other_failures(self):
        self.init("--profile", "economy")
        host = FakeHost(self.dir, broken={"b004-a2": "not_json.txt", "b004-a3": "missing"},
                        grouping={"g001": "missing", "g002": "not_json.txt"})
        done = host.run()
        self.assertEqual(done["dropped_agents"], 2)
        self.assertGreater(done["failures"], done["dropped_agents"])

    def test_next_after_the_end_repeats_the_full_summary(self):
        self.init("--profile", "economy")
        host = FakeHost(self.dir)
        done = host.run()
        self.assertEqual(host.cli("next", "--dir", str(self.dir)), done)

    def test_card_errors_name_the_real_cause(self):
        cases = {'{"id": "c01", "text": "x", "source": "y"}': "only the keys id and text",
                 '{"id": "卡片", "text": "x"}': "id must be",
                 '{"id": "c01", "text": " "}': "text must not be empty",
                 '{"id": "c01", "text": "x"}\n{"id": "c01", "text": "y"}': "repeated id"}
        for number, (body, expected) in enumerate(cases.items()):
            path = Path(self.tmp.name) / f"cards{number}.jsonl"
            path.write_text(body, encoding="utf-8")
            with self.assertRaises(bs.RunError) as caught:
                bs.read_cards(path)
            self.assertIn(expected, str(caught.exception))

    def test_run_without_cards(self):
        self.init(cards=False)
        host = FakeHost(self.dir)
        done = host.run()
        self.assertEqual(done["action"], "done")
        prompt = Path(host.actions[0]["tasks"][0]["prompt"]).read_text(encoding="utf-8")
        self.assertEqual(data_block(prompt, "Assigned cards:"), [])

    def test_economy_profile_stops_after_round2_without_asking(self):
        self.init("--profile", "economy")
        host = FakeHost(self.dir)
        host.run()
        self.assertNotIn("confirm_pressure", [a["action"] for a in host.actions])
        self.assertFalse(any(i["level"].startswith("pressure") for i in self.state()["ideas"].values()))

    def test_user_can_stop_before_pressure(self):
        self.init()
        host = FakeHost(self.dir, pressure="stop")
        done = host.run()
        self.assertEqual(done["action"], "done")
        self.assertFalse(any(i["level"].startswith("pressure") for i in self.state()["ideas"].values()))

    def test_ideas_per_agent_reaches_the_prompt(self):
        self.init("--ideas-per-agent", "4")
        action = FakeHost(self.dir).cli("next", "--dir", str(self.dir))
        prompt = Path(action["tasks"][0]["prompt"]).read_text(encoding="utf-8")
        self.assertIn("first list 4 idea titles", prompt)

    def test_report_self_check_rejects_a_missing_mechanism(self):
        self.init("--profile", "economy")
        FakeHost(self.dir).run()
        state = self.state()
        report = bs.render_report(state)
        bs.check_report(report, state)
        dropped = re.sub(r"^### 1\. .*$", "", report, count=1, flags=re.M)
        with self.assertRaises(bs.RunError):
            bs.check_report(dropped, state)
        state["mechanisms"].append({**state["mechanisms"][0], "mechanism_id": "m-999"})
        with self.assertRaises(bs.RunError):
            bs.check_report(report, state)

    def test_init_refuses_a_used_directory(self):
        self.init()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bs.main(["init", "--dir", str(self.dir), "--question", "q"]), 1)


class MethodLibraryTests(RunCase):
    def write_methods(self, rows):
        path = Path(self.tmp.name) / "methods.jsonl"
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
        return path

    def method_tasks(self):
        return [task for task in self.state()["tasks"].values() if task["kind"] == "generate" and task["method"]]

    def test_default_run_keeps_the_four_evaluated_methods(self):
        made = self.init()
        self.assertEqual(self.state()["methods"], bs.METHODS)
        self.assertEqual((made["methods"], made["evaluated_settings"]), (4, True))

    def test_bundled_library_extends_the_evaluated_methods_without_changing_them(self):
        library = {row["id"]: row["text"] for row in bs.read_methods(bs.METHOD_LIBRARY)}
        self.assertGreaterEqual(len(library), 16)
        for name, text in bs.METHODS.items():
            self.assertEqual(library[name], text)

    def test_library_run_gives_each_agent_a_method_no_one_had_before(self):
        made = self.init("--methods", "library", "--profile", "economy")
        self.assertEqual(made["evaluated_settings"], False)
        FakeHost(self.dir).run()
        state = self.state()
        tasks = self.method_tasks()
        self.assertEqual(len(tasks), 6)
        self.assertEqual(len({task["method"] for task in tasks}), 6)
        for task in tasks:
            prompt = Path(task["prompt"]).read_text(encoding="utf-8")
            lines = prompt.split("\n")
            self.assertEqual(lines[lines.index("Method instructions:") + 1], state["methods"][task["method"]])
        self.assertIn("method " + tasks[0]["method"], (self.dir / "report.md").read_text(encoding="utf-8"))

    def test_small_library_reuses_methods_only_after_all_were_used(self):
        rows = [{"id": f"m{n}", "text": f"method text {n}"} for n in range(1, 5)]
        self.init("--methods", str(self.write_methods(rows)), "--profile", "economy")
        FakeHost(self.dir).run()
        used = [task["method"] for task in sorted(self.method_tasks(), key=lambda task: task["batch"])]
        self.assertEqual(len(used), 6)
        self.assertEqual(set(used[:3]) | set(used[3:]), {"m1", "m2", "m3", "m4"})
        self.assertEqual(len(set(used[3:])), 3)

    def test_same_seed_and_library_reproduce_the_methods(self):
        self.init("--methods", "library", "--profile", "economy")
        FakeHost(self.dir).run()
        first = [(task["batch"], task["agent"], task["method"]) for task in self.method_tasks()]
        self.dir = Path(self.tmp.name) / "again"
        self.init("--methods", "library", "--profile", "economy")
        FakeHost(self.dir).run()
        self.assertEqual(first, [(task["batch"], task["agent"], task["method"]) for task in self.method_tasks()])

    def test_init_refuses_a_broken_method_file(self):
        good = {"id": "a", "text": "x"}
        for rows in ([good, {"id": "b", "text": "y"}],                       # fewer than one method per agent
                     [good, good, {"id": "c", "text": "z"}],                 # repeated id
                     [good, {"id": "b", "text": " "}, {"id": "c", "text": "z"}]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bs.main(["init", "--dir", str(self.dir), "--question", "q",
                                          "--methods", str(self.write_methods(rows))]), 1)
            self.assertFalse(self.dir.exists())


if __name__ == "__main__":
    unittest.main()
