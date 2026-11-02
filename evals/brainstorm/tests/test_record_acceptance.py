from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import io
from unittest.mock import patch


SAMPLES = Path(__file__).resolve().parents[1] / "exams/samples"


class RecordingAcceptance(unittest.TestCase):
    def test_sample_configuration_actually_exposes_the_injection_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.cli("record", "--exam-dir", SAMPLES / "injection", "--config", SAMPLES / "config.json",
                     "--seed", "19", "--run-dir", root)
            summary = json.loads((root / "injection_sample/sample/rep-1/run_summary.json").read_text())
            self.assertEqual(summary["guards"]["injection"], {"status": "checked", "violations": 0})
            self.assertTrue(all(row["cards"] == ["c1"] for row in summary["allocations"]["1:round1"]))

    def test_default_record_directory_and_unignored_output_warning(self):
        from evals.brainstorm.__main__ import main
        from evals.brainstorm.record import output_dir
        with patch("sys.argv", ["brainstorm", "record", "--exam-dir", str(SAMPLES / "open"),
                                "--config", str(SAMPLES / "config.json"), "--seed", "1"]), \
                patch("evals.brainstorm.record.record_batch", return_value={}) as record, \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(), 0)
        self.assertEqual(record.call_args.args[2], SAMPLES.parents[1] / "runs/sample")
        with patch("sys.stderr", new_callable=io.StringIO) as warning:
            output_dir(SAMPLES.parents[3] / "evals/tmp_run", "test", "test", 1)
            self.assertIn("not ignored", warning.getvalue())
        with patch("sys.stderr", new_callable=io.StringIO) as warning:
            output_dir(SAMPLES.parents[1] / "runs/sample", "test", "test", 1)
            self.assertEqual(warning.getvalue(), "")

    def test_help_registers_all_recording_commands(self):
        text = self.cli("--help").stdout
        for name in ("validate_exams", "record", "import_results"):
            self.assertIn(name, text)
            self.assertIn("usage:", self.cli(name, "--help").stdout)

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-m", "evals.brainstorm", *map(str, args)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def rows(self, path):
        return [json.loads(line, parse_float=Decimal) for line in path.read_text().splitlines()]

    def test_cli_validate_record_budget_resume_and_import_receipts_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIn("Validated 9 exams", self.cli("validate_exams", "--exam-dir", SAMPLES).stdout)
            full, partial = root / "full", root / "partial"
            before = {str(p): p.read_bytes() for p in SAMPLES.rglob("*") if p.is_file()}
            common = ["record", "--exam-dir", SAMPLES / "open", "--config", SAMPLES / "config.json", "--seed", "19"]
            self.cli(*common, "--run-dir", full)
            stopped = self.cli(*common, "--run-dir", partial, "--limit", ".03", expected=3)
            self.assertIn("shortfall_usd=", stopped.stderr)
            self.assertIn("pending_agents=", stopped.stderr)
            unit = Path("open_sample/sample/rep-1")
            prefix = self.rows(partial / unit / "ideas.jsonl")
            self.assertEqual(len(prefix), 20)
            self.cli(*common, "--run-dir", partial, "--limit", "1")
            events_before = (partial / "ledger.fake.jsonl").read_bytes()
            self.cli(*common, "--run-dir", partial, "--limit", "1")
            self.assertEqual(events_before, (partial / "ledger.fake.jsonl").read_bytes())
            self.assertEqual(self.rows(full / unit / "ideas.jsonl"), self.rows(partial / unit / "ideas.jsonl"))
            a = json.loads((full / unit / "run_summary.json").read_text(), parse_float=Decimal)
            b = json.loads((partial / unit / "run_summary.json").read_text(), parse_float=Decimal)
            for field in ("calls", "usd", "tokens", "ideas", "allocations", "failures", "guards", "complete"):
                self.assertEqual(a[field], b[field])
            self.assertEqual(self.rows(full / unit / "failures.jsonl"), [])
            self.assertEqual(self.rows(partial / unit / "failures.jsonl"), [])
            self.assertEqual(a["ideas"], 30)
            self.assertEqual(a["usd"], Decimal(".03"))
            self.assertEqual(a["calls"], 3)
            events = self.rows(partial / "ledger.fake.jsonl")
            self.assertEqual(sum(e["event"] == "reserve" for e in events), 3)
            self.assertEqual(sum(e["usd"] for e in events if e["event"] == "settle"), Decimal(".03"))
            calls = self.rows(partial / unit / "calls.jsonl")
            ideas = self.rows(partial / unit / "ideas.jsonl")
            self.assertEqual(len({c["call_key"] for c in calls}), 3)
            self.assertEqual(len({i["idea_id"] for i in ideas}), 30)
            for call in calls:
                group = [i for i in ideas if i["call_key"] == call["call_key"]]
                self.assertEqual(len(group), 10)
                for index, idea in enumerate(group):
                    self.assertEqual(idea["idea_id"], "i-" + hashlib.sha1((call["call_key"] + str(index)).encode()).hexdigest()[:12])
                    self.assertEqual(idea["model"], "fake_model")
                    assignment = b["allocations"]["1:round1"][idea["agent"] - 1]
                    self.assertEqual(idea["cards"], assignment["cards"])
                    self.assertEqual(idea["method"], assignment["method"])
            self.assertEqual(before, {str(p): p.read_bytes() for p in SAMPLES.rglob("*") if p.is_file()})
            imported = root / "imported"
            ledger = root / "receipts.fake.jsonl"
            args = ["import_results", "--ideas", full / unit / "ideas.jsonl", "--calls", full / unit / "calls.jsonl",
                    "--run-dir", imported, "--ledger", ledger]
            self.cli(*args)
            first = ledger.read_bytes()
            self.cli(*args)
            self.assertEqual(first, ledger.read_bytes())
            self.assertEqual(self.rows(imported / unit / "ideas.jsonl"), ideas)

    def test_cli_parse_retry_budget_stop_and_resume_preserves_attempt_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = json.dumps({"ideas": [{"title": "One", "text": "Use one divider", "cards_used": [], "decomposition": None}]})
            config = {"name": "retry", "default_model": "fake_model", "agents": 1,
                      "backend": {"kind": "fake", "responses": ["bad", good]}}
            path = root / "config.json"
            path.write_text(json.dumps(config))
            args = ["record", "--exam-dir", SAMPLES / "open", "--config", path, "--seed", "1", "--run-dir", root / "run"]
            self.cli(*args, "--limit", ".02", expected=3)
            # A fresh fake process uses the first scripted response on its first actual call.
            # Change only its local script via a separate direct recovery test (QA7/9); here
            # repeating two invalid responses demonstrates the terminal two-attempt ceiling.
            self.cli(*args, "--limit", "1")
            unit = root / "run/open_sample/retry/rep-1"
            calls = self.rows(unit / "calls.jsonl")
            self.assertEqual([c["attempt"] for c in calls], [1, 2])
            self.assertEqual(len(self.rows(unit / "ideas.jsonl")), 0)
            first = (root / "run/ledger.fake.jsonl").read_bytes()
            self.cli(*args, "--limit", "1")
            self.assertEqual(first, (root / "run/ledger.fake.jsonl").read_bytes())
