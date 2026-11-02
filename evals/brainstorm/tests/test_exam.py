import json
from pathlib import Path
import tempfile
import unittest

from evals.brainstorm.exam import load_exams, validate_exam


SAMPLES = Path(__file__).resolve().parents[1] / "exams/samples"


def exam_data(kind="open"):
    return {"id": kind + "_sample", "type": kind, "question": "Test only: reuse a box?",
            "constraints": [], "cards": "cards.jsonl", "planted": [], "hidden_answers": [],
            "wrong_premise": None, "created_after": None, "leak_check": None,
            "license": "Self authored test fixture; CC0"}


class ExamTests(unittest.TestCase):
    def test_probe_types_require_real_marker_cards_and_premise_requires_text(self):
        for kind in ("injection", "planted"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_exam(exam_data(kind), [{"id": "c1", "text": "A markerless card"}])
        for kind, probe_kind in (("injection", "color"), ("planted", "injection")):
            value = {**exam_data(kind), "planted": [{"card_id": "c1", "kind": probe_kind, "marker": "M"}]}
            with self.assertRaises(ValueError):
                validate_exam(value, [{"id": "c1", "text": "M"}])
        for kind, probe_kind in (("injection", "injection"), ("planted", "color")):
            value = {**exam_data(kind), "planted": [{"card_id": "c1", "kind": probe_kind, "marker": "M"}]}
            with self.assertRaises(ValueError):
                validate_exam(value, [{"id": "c1", "text": "marker absent"}])
            self.assertEqual(validate_exam(value, [{"id": "c1", "text": "M"}])["type"], kind)
        with self.assertRaises(ValueError):
            validate_exam(exam_data("wrong_premise"), [])
        with self.assertRaises(ValueError):
            validate_exam({**exam_data(), "check_sample": []}, [])

    def test_unicode_line_separator_inside_card_does_not_split_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "exam.json").write_text(json.dumps(exam_data()))
            (root / "cards.jsonl").write_text('{"id":"c1","text":"A\u2028B"}\n')
            self.assertEqual(load_exams(root)[0]["card_data"][0]["text"], "A\u2028B")

    def test_nine_public_test_only_samples(self):
        from evals.brainstorm.exam import EXAM_TYPES
        exams = load_exams(SAMPLES)
        self.assertEqual(len(exams), 9)
        self.assertEqual({e["type"] for e in exams}, set(EXAM_TYPES))
        self.assertTrue(all(e["question"].startswith("Test only") for e in exams))

    def test_bad_exam_fields_and_card_references_rejected(self):
        cards = [{"id": "c1", "text": "Test"}]
        cases = [("type", "unknown"), ("type", []), ("question", ""), ("license", " "),
                 ("constraints", "x"), ("hidden_answers", [None]), ("wrong_premise", False),
                 ("created_after", "2026-02-30"), ("check_samples", [3]),
                 ("planted", [{"card_id": "absent", "kind": "injection", "marker": "X"}]),
                 ("id", "../escape")]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_exam({**exam_data(), key: value}, cards)
        for key in exam_data():
            broken = exam_data()
            del broken[key]
            with self.subTest(missing=key), self.assertRaisesRegex(ValueError, "missing"):
                validate_exam(broken, cards)
        with self.assertRaisesRegex(ValueError, "created_after"):
            validate_exam(exam_data("forecast"), cards)
        for bad_cards in ([cards[0], cards[0]], [{"id": "c1", "text": ""}], [None]):
            with self.assertRaises(ValueError):
                validate_exam(exam_data(), bad_cards)

    def test_paths_and_malformed_files_fail_with_location(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            exam_path = root / "exam.json"
            card_path = root / "cards.jsonl"
            for content in ('{"id":', '[]\n', '{"id":"x"}\n', '{"id":"x","text":"y"}'):
                exam_path.write_text(json.dumps(exam_data()))
                card_path.write_text(content)
                with self.assertRaisesRegex(ValueError, "exam.json"):
                    load_exams(root)
            card_path.unlink()
            (Path(outside) / "cards.jsonl").write_text('{"id":"c1","text":"outside"}\n')
            card_path.symlink_to(Path(outside) / "cards.jsonl")
            with self.assertRaises(ValueError):
                load_exams(root)
            exam_path.write_text(json.dumps({**exam_data(), "cards": "../cards.jsonl"}))
            with self.assertRaisesRegex(ValueError, "inside"):
                load_exams(root)

    def test_duplicate_exams_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a", "b"):
                root = Path(tmp) / name
                root.mkdir()
                (root / "exam.json").write_text(json.dumps(exam_data()))
                (root / "cards.jsonl").write_text("")
            with self.assertRaisesRegex(ValueError, "duplicate exam"):
                load_exams(tmp)

    def test_load_external_directory_without_modifying_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "exam.json").write_text(json.dumps(exam_data()))
            (root / "cards.jsonl").write_text('{"id":"c1","text":"Test card"}\n')
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            exams = load_exams(root)
            self.assertEqual(exams[0]["card_data"], [{"id": "c1", "text": "Test card"}])
            self.assertEqual(exams[0]["question"], "Test only: reuse a box?")
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
