"""The README figures of think-compare agree with the result files."""

import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIGURES = ROOT / "docs" / "figures" / "compare"
PAGES = {"en": ROOT / "skills" / "think-compare" / "README.md",
         "zh": ROOT / "skills" / "think-compare" / "README.zh.md"}


def images(page):
    return re.findall(r"!\[[^\]]*\]\(([^)]+\.png)\)", page.read_text(encoding="utf-8"))


class FigureTests(unittest.TestCase):
    def test_chart_definitions_hold_the_numbers_of_the_result_file(self):
        spec = importlib.util.spec_from_file_location("compare_build_specs", FIGURES / "build_specs.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        built = module.build()
        self.assertEqual(sorted(built), sorted(p.name for p in (FIGURES / "src").glob("*.vl.json")))
        for name, body in built.items():
            self.assertEqual(json.loads((FIGURES / "src" / name).read_text(encoding="utf-8")), body, name)

    def test_every_image_exists_in_the_page_language_and_has_a_definition(self):
        for language, page in PAGES.items():
            sources = images(page)
            self.assertGreaterEqual(len(sources), 4, language)
            for source in sources:
                image = (page.parent / source).resolve()
                self.assertTrue(image.is_file(), f"{language}: {source}")
                self.assertTrue(image.name.endswith(f".{language}.png"), f"{language} page shows {image.name}")
                stem = image.name[:-len(".png")]
                self.assertTrue(any((FIGURES / "src" / f"{stem}{end}").is_file() for end in (".d2", ".vl.json")), stem)

    def test_both_pages_show_the_same_figures_in_the_same_order(self):
        names = {lang: [Path(s).name.rsplit(".", 2)[0] for s in images(page)] for lang, page in PAGES.items()}
        self.assertEqual(names["en"], names["zh"])


if __name__ == "__main__":
    unittest.main()
