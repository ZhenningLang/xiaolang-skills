"""One explicit, recorded replacement of a failed unit; nothing paid is erased and nothing retries by itself."""

import contextlib
from decimal import Decimal
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from evals.brainstorm import dedupe, e2e, judge, replace_unit, replacements, report, sheet
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetExceeded, BudgetLedger, LedgerError
from evals.brainstorm.exam import load_exams
from evals.brainstorm.loop import run_loop
from evals.brainstorm.record import read_file, read_rows, record_batch
from evals.brainstorm.replace_unit import ReplacementFailed
from evals.brainstorm.sheet import discover_units


ROOT = Path(__file__).resolve().parents[3]


def quiet(function, *args, **options):
    with contextlib.redirect_stderr(io.StringIO()):
        return function(*args, **options)


def bad_first(count):
    """The first `count` generation calls return junk, the rest are the normal scenario."""
    state = {"left": count}

    def respond(prompt, model, max_output_tokens):
        if state["left"]:
            state["left"] -= 1
            return "not json"
        return e2e.scenario(prompt, model, max_output_tokens)
    return respond


class Flow(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name).resolve()
        self.root = self.temp / "run"
        self.root.mkdir()
        self.plain, self.returned, self.classifier = e2e.write_inputs(self.root)
        self.exam = next(row for row in load_exams(e2e.EXAM_DIR) if row["id"] == "open_sample")
        self.ledger = BudgetLedger(self.root / "ledger.fake.jsonl", "50")
        self.generation = FakeBackend(responses=e2e.scenario)
        self.grouping = FakeBackend(responses=dedupe.fake_partition)
        self.judging = FakeBackend(responses=judge.fake_judge)

    def run_unit(self, config=None, rep=1, generation=None, grouping=None, ledger=None, seed=1):
        return quiet(run_loop, self.exam, config or self.returned, self.root, seed=seed, backend=generation or self.generation,
                     ledger=ledger or self.ledger, dedupe_config=self.classifier, dedupe_backend=grouping or self.grouping, rep=rep)

    def fail_unit(self, rep=1, failure="paid_error", config=None):
        with self.assertRaises((ValueError, LedgerError)):
            self.run_unit(config, rep, FakeBackend(failure=failure))

    def replace(self, failed=1, new=4, config=None, generation=None, grouping=None, ledger=None):
        return quiet(replace_unit.replace_unit, self.exam, config or self.returned, self.root, failed_rep=failed, new_rep=new,
                     backend=generation or self.generation, ledger=ledger or self.ledger, dedupe_config=self.classifier,
                     dedupe_backend=grouping or self.grouping)

    def events(self):
        return read_rows(self.ledger.path)

    def compare_sheet(self, sheet_id, configs, seed=7):
        quiet(sheet.make_sheet, self.root, self.exam, kind="score", sheet_id=sheet_id, seed=seed, configs=configs,
              backend=FakeBackend(responses=sheet.fake_rewrite), ledger=self.ledger, model="clf_alpha")
        key = read_file(self.root / "sheets" / sheet_id / "sheet.key.json")
        if key["items"]:
            sheet.merge_answers(self.root, sheet_id, {"sheet_id": sheet_id, "answers": [
                {"item_id": item, "unthought": True, "care": True, "fatal_reason": None, "want_to_try": False} for item in key["items"]]})
        return sheet.reveal(self.root, sheet_id)

    def report(self, **options):
        return quiet(report.build_report, self.root, [self.exam], [self.plain, self.returned], baseline=self.plain["name"],
                     candidate=self.returned["name"], decision_exam_ids=[self.exam["id"]], ledger_path=self.ledger.path,
                     exact_receipts=True, **options)


class OnlyFailedUnitsTests(Flow):
    def test_finished_unit_is_refused(self):
        self.run_unit()
        before = self.events()
        self.assertRaisesRegex(ValueError, "did not fail", self.replace)
        self.assertEqual(self.events(), before)
        self.assertEqual(replacements.load(self.root, self.exam["id"]), [])

    def test_unit_stopped_by_the_budget_is_refused_because_it_can_resume(self):
        small = BudgetLedger(self.ledger.path, "0.03")
        with self.assertRaises(BudgetExceeded):
            self.run_unit(ledger=small)
        with self.assertRaisesRegex(ValueError, "did not fail"):
            self.replace(ledger=small)

    def test_unknown_unit_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no recorded loop"):
            self.replace()

    def test_standalone_recording_is_refused(self):
        quiet(record_batch, self.exam, self.returned, self.root, seed=1, backend=FakeBackend(failure="paid_error"),
              ledger=self.ledger, level="baseline")
        with self.assertRaisesRegex(ValueError, "no recorded loop"):
            self.replace()

    def test_uncertain_call_must_be_reconciled_first(self):
        self.fail_unit(failure="unknown")
        with self.assertRaisesRegex(ValueError, "[Rr]econcile"):
            self.replace()
        self.assertEqual(replacements.load(self.root, self.exam["id"]), [])

    def test_blocked_neighbour_is_pointed_at_the_owner(self):
        with self.assertRaises(LedgerError):
            self.run_unit(self.returned, 1, grouping=FakeBackend(failure="paid_error"))
        with self.assertRaisesRegex(LedgerError, "already paid"):
            self.run_unit(self.returned, 2)
        with self.assertRaisesRegex(ValueError, "did not fail"):
            self.replace(failed=2)
        self.assertEqual(self.replace(failed=1)["status"], "complete")
        self.run_unit(self.returned, 2)

    def test_parse_failures_on_both_attempts_count_as_failed(self):
        with self.assertRaisesRegex(ValueError, "Generation failed"):
            self.run_unit(generation=FakeBackend(responses=bad_first(4)))
        self.assertEqual(self.replace()["status"], "complete")


class OnceOnlyTests(Flow):
    def test_success_records_who_stands_in_for_whom(self):
        self.fail_unit()
        result = self.replace()
        self.assertEqual(result["status"], "complete")
        self.assertEqual((result["failed_rep"], result["new_rep"]), (1, 4))
        rows = replacements.load(self.root, self.exam["id"])
        self.assertEqual([(row["config"], row["failed_rep"], row["new_rep"]) for row in rows], [("cfg_return", 1, 4)])
        self.assertEqual(rows[0]["stage"], "recording")

    def test_a_failed_unit_is_replaced_once(self):
        self.fail_unit()
        self.replace()
        before = self.events()
        self.assertRaisesRegex(ValueError, "already replaced", self.replace, new=5)
        self.assertEqual(self.events(), before)

    def test_repeating_the_same_finished_command_spends_nothing(self):
        self.fail_unit()
        self.replace()
        before = self.events()
        backend = FakeBackend(responses=e2e.scenario)
        self.assertEqual(self.replace(generation=backend)["status"], "complete")
        self.assertEqual((backend.calls, self.events()), (0, before))

    def test_failed_replacement_stops_and_cannot_be_replaced(self):
        self.fail_unit()
        with self.assertRaises(ReplacementFailed) as caught:
            self.replace(generation=FakeBackend(failure="paid_error"))
        self.assertIn("cannot be replaced", str(caught.exception))
        self.assertIn("rep 4", str(caught.exception))
        before = self.events()
        fresh = FakeBackend(responses=e2e.scenario)
        with self.assertRaises(ReplacementFailed):
            self.replace(generation=fresh)
        self.assertRaisesRegex(ValueError, "cannot be replaced", self.replace, failed=4, new=5, generation=fresh)
        self.assertRaisesRegex(ValueError, "already replaced", self.replace, new=5, generation=fresh)
        self.assertEqual((fresh.calls, self.events()), (0, before))

    def test_new_rep_must_be_fresh(self):
        self.fail_unit()
        self.run_unit(rep=2)
        for new, message in ((1, "itself"), (2, "already exists")):
            with self.subTest(new=new), self.assertRaisesRegex(ValueError, message):
                self.replace(new=new)
        self.assertEqual(replacements.load(self.root, self.exam["id"]), [])

    def test_retired_unit_cannot_run_again(self):
        self.fail_unit()
        self.replace()
        before = self.events()
        backend = FakeBackend(responses=e2e.scenario)
        with self.assertRaisesRegex(ValueError, "replaced"):
            self.run_unit(rep=1, generation=backend)
        with self.assertRaisesRegex(ValueError, "replaced"):
            quiet(record_batch, self.exam, self.returned, self.root, seed=1, backend=backend, ledger=self.ledger)
        self.assertEqual((backend.calls, self.events()), (0, before))

    def test_record_rejects_a_hand_edited_second_replacement(self):
        self.fail_unit()
        self.replace()
        path = self.root / self.exam["id"] / "replacements.jsonl"
        row = json.loads(path.read_text(encoding="utf-8"))
        row["new_rep"] = 5
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        with self.assertRaisesRegex(ValueError, "replacement"):
            replacements.load(self.root, self.exam["id"])


class NothingIsErasedTests(Flow):
    def test_ledger_only_grows_and_failed_outputs_stay(self):
        self.fail_unit()
        unit = self.root / self.exam["id"] / self.returned["name"] / "rep-1"
        kept = {path.name: path.read_bytes() for path in unit.iterdir()
                if path.name in ("ideas.jsonl", "calls.jsonl", "failures.jsonl", "run_summary.json")}
        before = self.events()
        self.replace()
        after = self.events()
        self.assertEqual(after[:len(before)], before)
        self.assertGreater(len(after), len(before))
        for name, content in kept.items():
            self.assertEqual((unit / name).read_bytes(), content, name)
        self.assertTrue(read_rows(unit / "calls.jsonl"))

    def test_other_ledger_is_refused(self):
        self.fail_unit()
        other = BudgetLedger(self.temp / "other.jsonl", "50")
        self.assertRaisesRegex(ValueError, "ledger", self.replace, ledger=other)
        self.assertFalse(other.path.exists())

    def test_changed_config_is_refused(self):
        self.fail_unit()
        changed = {**self.returned, "max_output_tokens": 999}
        with self.assertRaisesRegex(ValueError, "config"):
            self.replace(config=changed)

    def test_the_command_takes_no_seed_and_keeps_the_failed_units(self):
        self.fail_unit()
        self.replace()
        identity = read_file(self.root / self.exam["id"] / self.returned["name"] / "rep-4/loop_state.json")["state"]["identity"]
        self.assertEqual(identity["seed"], 1)
        parser = subprocess.run([sys.executable, "-m", "evals.brainstorm", "replace-unit", "--help"], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(parser.returncode, 0, parser.stderr)
        self.assertNotIn("--seed", parser.stdout)

    def test_new_rep_reserves_through_the_budget_and_resumes_after_a_stop(self):
        self.fail_unit()
        spent = self.ledger.status()["used"]
        tight = BudgetLedger(self.ledger.path, str(spent + Decimal("0.03")))
        with self.assertRaises(BudgetExceeded):
            self.replace(ledger=tight)
        rows = self.events()
        settled = [row["call_key"] for row in rows if row["event"] == "settle"]
        self.assertEqual(self.replace()["status"], "complete")
        after = self.events()
        self.assertEqual(after[:len(rows)], rows)
        keys = [row["call_key"] for row in after if row["event"] == "settle"]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(set(settled) <= set(keys))

    def test_max_paid_attempts_means_what_it_meant(self):
        self.assertEqual(dedupe.MAX_PAID_ATTEMPTS, 2)


class ReplacedSetTests(Flow):
    def full_run(self, failed_rep=1):
        """Three repetitions of both configs, one of them failed and replaced."""
        for config in (self.plain, self.returned):
            for rep in (1, 2, 3):
                if config is self.returned and rep == failed_rep:
                    self.fail_unit(rep)
                else:
                    self.run_unit(config, rep)
        self.replace(failed_rep, 4)
        for config in (self.plain, self.returned):
            for _, rep in discover_units(self.root, self.exam["id"], config["name"]):
                report.judge_unit(self.root, self.exam, config["name"], rep, backend=self.judging, ledger=self.ledger, model="clf_alpha")

    def test_discovery_swaps_the_failed_rep_for_the_new_one(self):
        self.fail_unit()
        self.run_unit(rep=2)
        self.assertEqual(discover_units(self.root, self.exam["id"], "cfg_return"), [("cfg_return", 1), ("cfg_return", 2)])
        self.replace()
        self.assertEqual(discover_units(self.root, self.exam["id"], "cfg_return"), [("cfg_return", 2), ("cfg_return", 4)])

    def test_failed_unit_blocks_the_sheet_until_it_is_replaced(self):
        for config in (self.plain, self.returned):
            for rep in (1, 2, 3):
                if config is self.returned and rep == 1:
                    self.fail_unit(rep)
                else:
                    self.run_unit(config, rep)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.compare_sheet("too_early", ["cfg_plain", "cfg_return"])
        self.replace()
        self.compare_sheet("after_swap", ["cfg_plain", "cfg_return"])
        units = read_file(self.root / "sheets/after_swap/sheet.key.json")["units"]
        self.assertEqual(set(units), {f"{name}/rep-{rep}" for name in ("cfg_plain",) for rep in (1, 2, 3)}
                         | {f"cfg_return/rep-{rep}" for rep in (2, 3, 4)})

    def test_score_guard_and_cost_counts_match_after_the_swap(self):
        self.full_run()
        self.compare_sheet("after_swap", ["cfg_plain", "cfg_return"])
        built = self.report()
        evidence = built["exams"][self.exam["id"]]["evidence"]
        for name in ("cfg_plain", "cfg_return"):
            self.assertEqual(len(evidence["scores"][name]), 3)
            self.assertEqual(len(evidence["guards"][name]), 3)
            self.assertEqual(len(evidence["cost_usd"][name]), 3)
        self.assertNotEqual(built["decision"]["decision"], "indeterminate", built["decision"]["reason"])
        self.assertEqual(sorted(built["exams"][self.exam["id"]]["units"]["cfg_return"]), [2, 3, 4])
        self.assertTrue(built["receipts"]["ok"], built["receipts"])
        self.assertEqual(sorted(built["exams"][self.exam["id"]]["comparisons"]), [1, 2, 3])

    def test_failed_cost_is_shown_but_stays_out_of_the_comparison(self):
        self.full_run()
        self.compare_sheet("after_swap", ["cfg_plain", "cfg_return"])
        built = self.report()
        row = built["replacements"][0]
        unit = self.root / self.exam["id"] / "cfg_return"
        failed = sum(entry["usd"] for entry in read_rows(unit / "rep-1/calls.jsonl"))
        new = sum(entry["usd"] for path in (unit / "rep-4").rglob("calls.jsonl") for entry in read_rows(path))
        self.assertGreater(failed, 0)
        self.assertEqual((row["failed_usd"], row["new_usd"]), (failed, new))
        self.assertEqual(row["status"], "complete")
        self.assertEqual(len(built["exams"][self.exam["id"]]["evidence"]["cost_usd"]["cfg_return"]), 3)
        self.assertEqual(built["costs"]["total"]["paid_error_calls"], read_file(unit / "rep-1/run_summary.json")["calls"])

    def test_summary_and_detail_name_the_unit_and_the_money(self):
        self.full_run()
        self.compare_sheet("after_swap", ["cfg_plain", "cfg_return"])
        built = self.report()
        row = built["replacements"][0]
        text = (self.root / "report.md").read_text(encoding="utf-8")
        summary = text.split("## 先看这里\n", 1)[1].split("\n## ", 1)[0]
        detail = text.split("<summary>14. 单元替换</summary>", 1)[1]
        for block in (summary, detail):
            self.assertIn("cfg_return", block)
            self.assertRegex(block, r"(重复|第) ?1")
            self.assertRegex(block, r"(重复|第) ?4")
            self.assertIn(report.money(row["failed_usd"]), block)
            self.assertIn(report.money(row["new_usd"]), block)
        html_page = (self.root / "report.html").read_text(encoding="utf-8")
        self.assertIn("单元替换", html_page)

    def test_failed_replacement_shows_in_the_report_and_the_decision_waits(self):
        for config in (self.plain, self.returned):
            for rep in (1, 2, 3):
                if config is self.returned and rep == 1:
                    self.fail_unit(rep)
                else:
                    self.run_unit(config, rep)
        with self.assertRaises(ReplacementFailed):
            self.replace(generation=FakeBackend(failure="paid_error"))
        built = quiet(report.build_report, self.root, [self.exam], [self.plain, self.returned], baseline="cfg_plain",
                      candidate="cfg_return", decision_exam_ids=[self.exam["id"]], ledger_path=self.ledger.path)
        self.assertEqual(built["decision"]["decision"], "indeterminate")
        self.assertEqual(built["replacements"][0]["status"], "failed")
        self.assertIn("不能再替换", "\n".join(built["summary"]))

    def test_sheet_issued_before_the_swap_no_longer_counts(self):
        for config in (self.plain, self.returned):
            for rep in (1, 2, 3):
                self.run_unit(config, rep)
                report.judge_unit(self.root, self.exam, config["name"], rep, backend=self.judging, ledger=self.ledger, model="clf_alpha")
        self.compare_sheet("before_swap", ["cfg_plain", "cfg_return"])
        path = self.root / self.exam["id"] / "cfg_return/rep-1/judges/constraint/state.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        state = envelope["state"]
        state["invalid_response"] = True
        state["result"] = None
        encoded = json.dumps(state)
        import hashlib
        path.write_text(json.dumps({"digest": hashlib.sha256(encoded.encode()).hexdigest(), "state": state}), encoding="utf-8")
        self.assertEqual(self.replace()["status"], "complete")
        built = self.report()
        self.assertEqual(built["decision"]["decision"], "indeterminate")
        self.assertIn("替换", built["exams"][self.exam["id"]]["evidence"]["answers_reason"])

    def test_ungrouped_ideas_of_the_failed_unit_stay_on_disk_and_out_of_the_table(self):
        with self.assertRaisesRegex(ValueError, "Generation failed"):
            self.run_unit(generation=FakeBackend(responses=bad_first(2)))
        unit = self.root / self.exam["id"] / "cfg_return/rep-1"
        stored = read_rows(unit / "ideas.jsonl")
        self.assertTrue(stored, "the second agent's output must exist for this test to mean anything")
        self.run_unit(self.returned, 2)
        with self.assertRaisesRegex(ValueError, "replace-unit"):
            sheet.load_table(self.root, self.exam["id"], sheet.load_ideas(self.root, self.exam["id"]))
        self.replace()
        ideas = sheet.load_ideas(self.root, self.exam["id"])
        self.assertFalse({row["idea_id"] for row in stored} & set(ideas))
        sheet.load_table(self.root, self.exam["id"], ideas)
        self.assertEqual(read_rows(unit / "ideas.jsonl"), stored)
        built = self.report()
        self.assertEqual(built["replacements"][0]["ungrouped_ideas"], len(stored))
        self.assertIn(f"{len(stored)} 条", (self.root / "report.md").read_text(encoding="utf-8").split("<summary>14. 单元替换</summary>", 1)[1])

    def test_grouped_ideas_of_the_failed_unit_stay_in_the_full_view(self):
        self.run_unit(self.returned, 2)

        def respond(prompt, model, max_output_tokens):
            if "Generation level: round2." in prompt:
                return "not json"
            return e2e.scenario(prompt, model, max_output_tokens)
        with self.assertRaisesRegex(ValueError, "Generation failed"):
            self.run_unit(generation=FakeBackend(responses=respond))
        unit = self.root / self.exam["id"] / "cfg_return/rep-1"
        grouped = {row["idea_id"] for row in read_rows(unit / "ideas.jsonl") if row["level"] != "round2"}
        self.assertTrue(grouped)
        self.replace()
        self.assertTrue(grouped <= set(sheet.load_ideas(self.root, self.exam["id"])))


class ConstraintFailureTests(Flow):
    def test_settled_constraint_call_is_a_failed_unit_and_its_receipt_is_synced(self):
        self.run_unit()
        with self.assertRaises(LedgerError):
            report.judge_unit(self.root, self.exam, "cfg_return", 1, backend=FakeBackend(failure="unknown"), ledger=self.ledger, model="clf_alpha")
        with self.assertRaisesRegex(ValueError, "[Rr]econcile"):
            self.replace()
        key = next(key for key, row in report.read_ledger(self.ledger.path).items() if row["event"] == "uncertain")
        self.ledger.settle(key, Decimal("0.004"))
        result = self.replace()
        self.assertEqual((result["status"], result["stage"]), ("complete", "constraint"))
        rows = read_rows(self.root / self.exam["id"] / "cfg_return/rep-1/judges/constraint/calls.jsonl")
        self.assertEqual([(row["usd"], row["outcome"]) for row in rows], [(Decimal("0.004"), "paid_error")])
        self.assertTrue(report.reconcile(report.load_receipts(self.root), report.read_ledger(self.ledger.path))["ok"])


class CommandLineTests(Flow):
    def command(self, *extra):
        return subprocess.run(
            [sys.executable, "-m", "evals.brainstorm", "replace-unit", "--run-dir", str(self.root), "--exam-dir", str(e2e.EXAM_DIR),
             "--exam-id", "open_sample", "--config", str(self.root / "e2e_inputs/cfg_return.json"),
             "--dedupe-config", str(self.root / "e2e_inputs/cfg_classifier.json"), "--ledger", str(self.ledger.path), *extra],
            cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_explicit_flags_are_required(self):
        self.assertEqual(self.command().returncode, 2)
        self.assertEqual(self.command("--failed-rep", "1").returncode, 2)

    def test_runs_refuses_and_prints_the_outcome(self):
        self.fail_unit()
        before = self.events()
        done = self.command("--failed-rep", "1", "--new-rep", "4", "--reason", "provider outage")
        self.assertEqual(done.returncode, 0, done.stderr)
        printed = json.loads(done.stdout)
        self.assertEqual((printed["status"], printed["failed_rep"], printed["new_rep"]), ("complete", 1, 4))
        self.assertEqual(self.events()[:len(before)], before)
        self.assertEqual(replacements.load(self.root, "open_sample")[0]["reason"], "provider outage")
        again = self.command("--failed-rep", "1", "--new-rep", "5")
        self.assertEqual(again.returncode, 1)
        self.assertIn("already replaced", again.stderr)

    def test_nothing_starts_a_replacement_by_itself(self):
        self.fail_unit()
        with self.assertRaises(ValueError):
            self.run_unit(rep=1)
        self.assertEqual(replacements.load(self.root, "open_sample"), [])
        self.assertFalse((self.root / "open_sample/cfg_return/rep-2").exists())


class NoUnitStagesTests(Flow):
    """Calibration, replay and rewriting have no unit to replace; each has its own explicit way forward."""

    def test_settled_rewrite_call_recovers_with_a_new_attempt(self):
        for config in (self.plain, self.returned):
            self.run_unit(config, 1)
        options = dict(kind="score", sheet_id="redo", seed=7, configs=["cfg_plain", "cfg_return"], ledger=self.ledger, model="clf_alpha")
        with self.assertRaises(LedgerError):
            quiet(sheet.make_sheet, self.root, self.exam, backend=FakeBackend(failure="paid_error"), **options)
        with self.assertRaisesRegex(LedgerError, "already paid"):
            quiet(sheet.make_sheet, self.root, self.exam, backend=FakeBackend(responses=sheet.fake_rewrite), **options)
        quiet(sheet.make_sheet, self.root, self.exam, backend=FakeBackend(responses=sheet.fake_rewrite), rewrite_attempt=2, **options)
        self.assertTrue((self.root / "sheets/redo/sheet.json").is_file())

    def test_settled_calibration_call_recovers_under_a_new_name(self):
        self.run_unit(self.plain, 1)
        ideas = read_rows(self.root / self.exam["id"] / "cfg_plain/rep-1/ideas.jsonl")
        human = {"exam_id": self.exam["id"], "groups": [[row["idea_id"]] for row in ideas]}
        options = dict(exam_id=self.exam["id"], seed=1, ledger=self.ledger, model_a="clf_alpha", model_b="clf_beta",
                       allowed_models=["clf_alpha", "clf_beta"])
        with self.assertRaises(LedgerError):
            quiet(dedupe.calibrate, ideas, human, self.root, backend=FakeBackend(failure="paid_error"), name="first", **options)
        with self.assertRaisesRegex(LedgerError, "already paid"):
            quiet(dedupe.calibrate, ideas, human, self.root, backend=self.grouping, name="first", **options)
        result = quiet(dedupe.calibrate, ideas, human, self.root, backend=self.grouping, name="second", **options)
        self.assertEqual(result["status"], "complete")


class ReadmeTests(unittest.TestCase):
    def test_readme_explains_the_replacement_rules(self):
        text = (ROOT / "evals/README.md").read_text(encoding="utf-8")
        for needle in ("replace-unit", "--failed-rep", "--new-rep", "only once", "report.html", "--rewrite-attempt", "__drop_"):
            self.assertIn(needle, text)
        self.assertNotIn("there is no recovery path yet", text)
        self.assertNotIn("recovery policy is still undecided", text)


if __name__ == "__main__":
    unittest.main()
