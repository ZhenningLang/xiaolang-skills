import random
import os
import subprocess
import sys
import unittest

from evals.brainstorm.allocate import allocate
from evals.brainstorm.config import validate_config


class AllocateTests(unittest.TestCase):
    def test_modes_floor_and_empty_cards(self):
        exam = {"card_data": [{"id": "c1", "text": "1"}, {"id": "c2", "text": "2"}]}
        base = {"name": "test", "default_model": "m", "agents": 4, "methods": {"a": "A", "b": "B"}}
        for mode, expected in (("none", [None] * 4), ("same", ["a"] * 4),
                               ("different", ["a", "b", "a", "b"])):
            rows = allocate(exam, validate_config({**base, "method_mode": mode}), 0)
            self.assertEqual([r["method"] for r in rows], expected)
        for mode, ratio, expected in (("none", 1, 0), ("all", 0, 2),
                                      ("proportion", 0, 0), ("proportion", .2, 0),
                                      ("proportion", 1, 2)):
            config = validate_config({**base, "materials_mode": mode, "materials_proportion": ratio})
            self.assertTrue(all(len(r["cards"]) == expected for r in allocate(exam, config, 1)))
            self.assertTrue(all(r["cards"] == [] for r in allocate({"card_data": []}, config, 1)))

    def test_python_hash_seed_does_not_change_assignment(self):
        script = """import json
from evals.brainstorm.allocate import allocate
from evals.brainstorm.config import validate_config
e={'card_data':[{'id':str(i),'text':'x'} for i in range(12)]}
c=validate_config({'name':'t','default_model':'m','models':['n','m'],'materials_mode':'proportion','materials_proportion':.5})
print(json.dumps(allocate(e,c,42),sort_keys=True))
"""
        outputs = [subprocess.check_output([sys.executable, "-c", script], env={**os.environ, "PYTHONHASHSEED": seed},
                                            timeout=10) for seed in ("1", "99")]
        self.assertEqual(outputs[0], outputs[1])

    def test_reproducible_assignments_without_global_rng_mutation(self):
        exam = {"card_data": [{"id": f"c{i}", "text": str(i)} for i in range(20)]}
        config = validate_config({"name": "test", "default_model": "m", "models": ["n", "m"],
                                  "agents": 12, "method_mode": "different",
                                  "methods": {"reverse": "Reverse", "analogy": "Analogy"},
                                  "materials_mode": "proportion", "materials_proportion": .3})
        before = random.getstate()
        first = allocate(exam, config, 21)
        self.assertEqual(first, allocate(exam, config, 21))
        self.assertNotEqual(first, allocate(exam, config, 22))
        self.assertEqual(before, random.getstate())
        self.assertTrue(all(len(row["cards"]) == 6 for row in first))
