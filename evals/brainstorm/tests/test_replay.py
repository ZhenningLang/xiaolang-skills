"""Nested agent sampling preserves every recorded stage and input identity."""

import unittest

from evals.brainstorm import replay
from evals.brainstorm.tests.test_dedupe import idea


class ReplayTests(unittest.TestCase):
    def test_nested_agent_samples_preserve_all_stages_and_units(self):
        rows = [idea(agent * 10 + stage, agent=agent, level=level, batch=stage + 1)
                for agent in range(1, 21) for stage, level in enumerate(['baseline', 'round1', 'round2'])]
        rows[-1].update(returned=True, returned_from=rows[-2]['idea_id'])
        other = [idea(999, agent=1, config='other'), idea(998, rep=2, agent=1)]
        first = replay.sample_agents(rows + other, exam_id='sample', config='test', rep=1,
                                     seed=42, sizes=[5, 10, 20, 21])
        again = replay.sample_agents(other[::-1] + rows, exam_id='sample', config='test', rep=1,
                                     seed=42, sizes=[5, 10, 20, 21])
        self.assertEqual(first, again)
        self.assertEqual(len(first['5']['agents']), 5)
        self.assertEqual(len(first['10']['agents']), 10)
        self.assertEqual(len(first['20']['agents']), 20)
        self.assertEqual(first['5']['agents'], first['10']['agents'][:5])
        self.assertEqual(first['10']['agents'], first['20']['agents'][:10])
        self.assertEqual(len(first['5']['idea_ids']), 15)
        self.assertEqual(first['20']['idea_ids'], [r['idea_id'] for r in rows])
        self.assertEqual(first['21']['status'], 'not_evaluated')
        self.assertEqual(first['21']['available'], 20)

    def test_sampling_rejects_duplicate_ids_or_invalid_sizes(self):
        row = idea(1)
        for rows, sizes in [([row, row], [1]), ([row], [0]), ([row], [True]), ([row], [1, 1])]:
            with self.assertRaises(ValueError):
                replay.sample_agents(rows, exam_id='sample', config='test', rep=1, seed=42, sizes=sizes)
