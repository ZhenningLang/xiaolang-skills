"""Run the suite once; skipped or undiscovered cases are not a passing gate."""

from pathlib import Path
import sys
import unittest


def run(suite=None):
    if suite is None:
        root = Path(__file__).resolve().parents[3]
        suite = unittest.defaultTestLoader.discover(str(root / "evals/brainstorm/tests"), top_level_dir=str(root))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped or not result.testsRun:
        print("FAIL: skipped or undiscovered tests cannot satisfy verification", file=sys.stderr)
    return int(not result.wasSuccessful() or bool(result.skipped) or not result.testsRun)


if __name__ == "__main__":
    raise SystemExit(run())
