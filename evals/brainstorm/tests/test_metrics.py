"""Independent arithmetic oracles for recorded observations."""

from copy import deepcopy
from decimal import Decimal
import math
import unittest

from evals.brainstorm import metrics
from evals.brainstorm.tests.test_dedupe import idea


def table_for(rows, groups):
    by_id = {row['idea_id']: row for row in rows}
    return {'exam_id': 'sample', 'mechanisms': [
        {'mechanism_id': f'm-{n:03d}', 'label': f'Mechanism {n}', 'idea_ids': ids,
         'representative': ids[0], 'in_baseline': any(by_id[key]['level'] == 'baseline' for key in ids)}
        for n, ids in enumerate(groups, 1)]}


def receipt(row, total=10, **changes):
    return {key: row[key] for key in ('call_key', 'exam_id', 'config', 'rep', 'agent', 'level', 'model')} | {
        'purpose': 'rewrite' if row['returned'] else 'generate', 'backend': 'fake', 'attempt': 1,
        'started_at': None, 'duration_ms': 7, 'tokens': {'input': total, 'cache_write': 0,
        'cache_read': 100, 'output': 0}, 'usd': 0.01, 'usd_estimated': False, 'outcome': 'ok', **changes}


class MetricsTests(unittest.TestCase):
    def test_missing_receipts_and_measurements_never_certify_zero_complete_cost(self):
        row = idea(500)
        complete = {'ideas': [row], 'table': table_for([row], [[row['idea_id']]]),
                    'summary': {'complete': True, 'calls': 1, 'ideas': 1, 'failures': 0}, 'calls_present': True}
        for present, calls in [(False, []), (True, []), (True, [receipt(row, call_key='unrelated')])]:
            data = {**complete, 'calls': calls, 'calls_present': present}
            result = metrics.compute_unit(data, exam_id='sample', config='test', rep=1)
            costs = result['costs']['source_recording']
            for key in ('usd', 'tokens', 'duration_ms'):
                self.assertEqual(costs[key]['status'], 'not_evaluated')
                self.assertIsNone(costs[key]['value'])
            self.assertIn('Call', metrics.summary_text(result))
        for changes, key in [({'usd': None}, 'usd'), ({'tokens': {}}, 'tokens')]:
            data = {**complete, 'calls': [receipt(row, **changes)]}
            result = metrics.compute_unit(data, exam_id='sample', config='test', rep=1)
            self.assertIsNone(result['costs']['source_recording'][key]['value'])
        empty = metrics.compute_unit({'ideas': [], 'table': table_for([], []), 'calls': [],
            'summary': {'complete': True, 'calls': 0, 'ideas': 0, 'failures': 0}}, exam_id='sample', config='test', rep=1)
        self.assertEqual(empty['costs']['source_recording']['usd']['value'], 0)

    def test_incomplete_loop_is_not_a_completed_recording_and_unmeasured_baseline_is_unknown(self):
        data = self.comparison_input('a', [4, 7])
        data.update(loop_present=True, loop_complete=False, loop_projection_current=True)
        data['summary']['guards'] = {'injection': {'status': 'checked', 'violations': 0}}
        result = metrics.compute_unit(data, exam_id='sample', config='a', rep=1)
        self.assertTrue(all(entry['status'] == 'not_evaluated' for entry in result['metrics'].values()))
        self.assertEqual(result['guards']['injection']['status'], 'not_evaluated')
        self.assertIsNone(result['guards']['injection']['violations'])
        compared = metrics.compare_units(data, self.comparison_input('b', [5, 6]), seed=0)
        self.assertEqual({entry['status'] for entry in compared.values()}, {'not_evaluated'})
        baseline = metrics.exhaustion([{'level': 'baseline', 'status': 'ok', 'new_mechanism_ids': []}])
        self.assertIsNone(baseline['baseline']['value'])
        self.assertEqual(baseline['baseline']['status'], 'not_evaluated')

    def test_level_growth_needs_every_committed_batch_and_a_closed_level(self):
        zero = {'level': 'round1', 'status': 'ok', 'new_mechanism_ids': [], 'fallback_ids': []}
        added = {**zero, 'new_mechanism_ids': ['A']}
        pending = {**zero, 'status': 'not_evaluated', 'new_mechanism_ids': None}
        for second in [added, pending]:
            result = metrics.exhaustion([zero], expected=[zero, second], complete=second is added)
            self.assertIsNone(result['round1']['value'])
            self.assertEqual(result['round1']['status'], 'not_evaluated')
        self.assertEqual(metrics.exhaustion([zero, added], expected=[zero, added])['round1']['value'], 1)
        # No pending row has been created yet: an unfinished latest level is still open.
        self.assertIsNone(metrics.exhaustion([zero], expected=[zero], complete=False)['round1']['value'])

    def test_incomplete_summary_does_not_prove_an_observed_empty_unit(self):
        data = {'ideas': [], 'calls': [], 'table': table_for([], []), 'summary': {'complete': True}}
        result = metrics.compute_unit(data, exam_id='sample', config='test', rep=1)
        self.assertEqual(result['completion']['status'], 'not_evaluated')
        self.assertIsNone(result['metrics']['unique_mechanisms']['value'])
        self.assertIsNone(result['costs']['source_recording']['usd']['value'])

    def test_partial_partition_is_local_but_bad_members_still_fail(self):
        rows = [idea(550), idea(551, config='pending')]
        partial = table_for(rows, [[rows[0]['idea_id']]])
        ready = metrics.mechanism_metrics(rows, partial, exam_id='sample', config='test', rep=1)
        self.assertEqual(ready['unique_mechanisms']['value'], 1)
        pending = metrics.mechanism_metrics(rows, partial, exam_id='sample', config='pending', rep=1)
        self.assertTrue(all(entry['value'] is None for entry in pending.values()))
        for members in [[rows[0]['idea_id']] * 2, ['unknown']]:
            invalid = deepcopy(partial)
            invalid['mechanisms'][0]['idea_ids'] = members
            with self.assertRaises(ValueError):
                metrics.mechanism_metrics(rows, invalid, exam_id='sample', config='test', rep=1)

    def test_agent_overlap_ignores_repeated_hits_and_same_agent_number_in_other_units(self):
        rows = [idea(600 + i, agent=agent) for i, agent in enumerate([1, 1, 1, 2, 2, 1, 2])]
        rows[5]['rep'] = 2
        rows[6]['config'] = 'other'
        ids = [row['idea_id'] for row in rows]
        result = metrics.mechanism_metrics(rows, table_for(rows, [[ids[0], ids[1], ids[6]],
            [ids[2], ids[3]], [ids[4]], [ids[5]]]), exam_id='sample', config='test', rep=1)
        self.assertEqual(result['agent_overlap']['value'], 1 / 3)
        self.assertEqual(result['unique_ratio']['value'], 3 / 5)

    def test_four_call_sampling_obeys_literal_permutations_for_two_seeds(self):
        sides = []
        for config in ['a', 'b']:
            rows = [idea(700 + i + (100 if config == 'b' else 0), config=config) for i in range(8)]
            keys = [config + suffix for suffix in ['001', '010', '100', '111']]
            for i, row in enumerate(rows):
                row['call_key'] = keys[i // 2]
            sides.append({'ideas': rows, 'calls': [receipt(rows[i]) for i in [0, 2, 4, 6]],
                          'summary': {'complete': True, 'calls': 4, 'ideas': 8, 'failures': 0},
                          'table': table_for(rows, [[r['idea_id']] for r in rows])})
        seen = []
        # Independent frozen permutations of [0,1,2,3]: seed0=[2,0,1,3], seed1=[3,0,2,1].
        for seed, indices in [(0, [4, 5, 0, 1]), (1, [6, 7, 0, 1])]:
            result = metrics.compare_units(*sides, seed=seed, idea_count=3, token_cap=20)
            for side, data in zip(['a', 'b'], sides):
                self.assertEqual(result['same_ideas'][side]['idea_ids'], [data['ideas'][i]['idea_id'] for i in indices[:3]])
                self.assertEqual(result['same_tokens'][side]['idea_ids'], [data['ideas'][i]['idea_id'] for i in indices])
                self.assertEqual(result['same_tokens'][side]['call_keys'],
                                 [data['ideas'][indices[0]]['call_key'], data['ideas'][indices[2]]['call_key']])
            self.assertEqual(metrics.compare_units(*sides, seed=seed, idea_count=3, token_cap=20), result)
            seen.append(result['same_tokens']['a']['call_keys'])
        self.assertNotEqual(seen[0], seen[1])

    def test_unit_baseline_counts_and_missing_partition(self):
        rows = [idea(i, agent=(i % 3) + 1) for i in range(7)]
        rows[5]['level'] = 'baseline'
        rows[6].update(level='baseline', config='other', rep=2)
        ids = [row['idea_id'] for row in rows]
        table = table_for(rows, [[ids[0], ids[1], ids[5]], [ids[2], ids[3], ids[6]], [ids[4]]])
        result = metrics.mechanism_metrics(rows, table, exam_id='sample', config='test', rep=1)
        self.assertEqual(result['unique_mechanisms']['value'], 3)
        self.assertEqual(result['unique_ratio']['value'], 3 / 5)
        self.assertEqual(result['outside_baseline_ratio']['value'], 2 / 3)
        self.assertEqual(result['outside_baseline_ratio']['baseline_scope'], 'unit')
        for options in ({'table': None}, {'complete': False}, {'fallback_ids': [ids[0]]}):
            args = {'table': table, **options}
            missing = metrics.mechanism_metrics(rows, **args, exam_id='sample', config='test', rep=1)
            self.assertTrue(all(value['status'] == 'not_evaluated' for value in missing.values()))
            self.assertTrue(all(value['value'] is None for value in missing.values()))
        bad = deepcopy(table)
        bad['mechanisms'][0]['idea_ids'].append(ids[0])
        with self.assertRaises(ValueError):
            metrics.mechanism_metrics(rows, bad, exam_id='sample', config='test', rep=1)
        with self.assertRaises(ValueError):
            metrics.mechanism_metrics(rows + rows[:1], table, exam_id='sample', config='test', rep=1)

    def test_overlap_and_bias_corrected_chao_oracles(self):
        rows = [idea(i, agent=a) for i, a in enumerate([1, 1, 2, 2, 3, 3, 3])]
        ids = [r['idea_id'] for r in rows]
        result = metrics.mechanism_metrics(rows, table_for(rows, [[ids[0], ids[4]],
            [ids[1], ids[2], ids[5]], [ids[3], ids[6]]]), exam_id='sample', config='test', rep=1)
        self.assertAlmostEqual(result['agent_overlap']['value'], 5 / 9)
        rows = [idea(i) for i in range(5)]
        ids = [r['idea_id'] for r in rows]
        for groups, total, remaining, f1, f2 in (([ids[:2], ids[2:3], ids[3:4], ids[4:]], 5.5, 1.5, 3, 1),
                                               ([[key] for key in ids[:3]], 6, 3, 3, 0)):
            subset = rows if f2 else rows[:3]
            result = metrics.mechanism_metrics(subset, table_for(subset, groups),
                                               exam_id='sample', config='test', rep=1)
            self.assertEqual(result['chao1'], {'value': total, 'status': 'ok',
                'variant': 'bias_corrected_chao1', 'f1': f1, 'f2': f2})
            self.assertEqual(result['remaining_space']['value'], remaining)
            self.assertIsNone(result['agent_overlap']['value'])

    def test_text_similarity_mst_and_human_pool(self):
        result = metrics.text_metrics(['ab', 'ab', 'cd'], ids=['z', 'a', 'b'])
        self.assertAlmostEqual(result['pair_similarity']['value'], 1 / 3)
        self.assertEqual(result['mst']['value'], 1)
        self.assertEqual(result['mst']['mean_edge'], .5)
        self.assertEqual(result['mst']['edge_count'], 2)
        self.assertEqual(result['mst']['edges'], [['a', 'z'], ['a', 'b']])
        self.assertIn('粗估', result['mst']['warning'])
        self.assertAlmostEqual(metrics.text_metrics(['ＡＢ', 'ab'])['pair_similarity']['value'], 1)
        # df(ab)=2, df(ba)=1: the second document is ab,ba,ab.
        self.assertAlmostEqual(metrics.text_metrics(['ab', 'abab'])['pair_similarity']['value'],
                               2 / math.sqrt(4 + (1 + math.log(1.5)) ** 2))
        human = metrics.text_metrics(['ab', 'cd'], human_pool=['ab', 'ef'])['human_pool_distance']
        self.assertEqual(human['distances'], [0, 1])
        self.assertEqual(human['value'], .5)
        for texts in ([], ['ab'], ['a', 'ab'], ['', 'ab']):
            self.assertEqual(metrics.text_metrics(texts)['mst']['status'], 'not_evaluated')
        for pool in (None, []):
            self.assertIsNone(metrics.text_metrics(['ab', 'cd'], human_pool=pool)['human_pool_distance']['value'])
        with self.assertRaises(ValueError):
            metrics.text_metrics(['ab'], human_pool=[1])

    def test_type_specific_labels_are_independent_and_complete(self):
        rows = [idea(i, agent=[1, 1, 2, 3][i], text=['COLOR COLOR', 'plain', 'COLOR', 'plain'][i])
                for i in range(4)]
        ids = [r['idea_id'] for r in rows]
        rows[1].update(cards=['c1'], cards_used=['c1'])
        exam = {'id': 'sample', 'wrong_premise': 'A premise',
                'planted': [{'kind': 'color', 'marker': 'COLOR', 'card_id': 'c1'}]}
        evidence = {'exam_id': 'sample', 'wrong_premise': 'A premise',
                    'challenged': dict(zip(ids, [True, False, True, False]))}
        subproblems = {'exam_id': 'sample', 'targets': ['s1', 's2', 's3'],
                       'matches': dict(zip(ids, [['s1'], ['s1'], ['s3'], []]))}
        result = metrics.type_metrics(rows, exam, premise=evidence, subproblems=subproblems)
        self.assertEqual(result['color_ratio']['value'], .5)
        self.assertEqual(result['premise_challenge_ratio']['value'], 2 / 3)
        self.assertEqual(result['subproblem_coverage']['value'], 2 / 3)
        evidence['challenged'].pop(ids[0])
        self.assertIsNone(metrics.type_metrics(rows, exam, premise=evidence)['premise_challenge_ratio']['value'])
        evidence['challenged']['unknown'] = True
        with self.assertRaises(ValueError):
            metrics.type_metrics(rows, exam, premise=evidence)
        self.assertIsNone(metrics.type_metrics(rows, exam)['subproblem_coverage']['value'])

    def test_costs_missing_measurements_and_guard_states(self):
        row = idea(1)
        calls = [receipt(row, tokens={'input': 2, 'cache_write': 3, 'cache_read': 100, 'output': 5})]
        cost = metrics.call_costs(calls)
        self.assertEqual(cost['tokens']['value'], {'input': 2, 'cache_write': 3, 'cache_read': 100,
                                                   'output': 5, 'total': 10})
        self.assertEqual(cost['duration_ms']['value'], 7)
        self.assertEqual(cost['usd']['value'], Decimal('.01'))
        for status, count in [('checked', 0), ('not_applicable', None), ('not_evaluated', None), ('not_evaluated', 1)]:
            guard = {'status': status, 'violations': count}
            self.assertEqual(metrics.guard_evidence({'guards': {'injection': guard}})['injection'], guard)
        calls[0].update(tokens={}, duration_ms=None, outcome='paid_error', usd_estimated=True)
        cost = metrics.call_costs(calls)
        self.assertIsNone(cost['tokens']['value'])
        self.assertIsNone(cost['duration_ms']['value'])
        self.assertEqual(cost['estimated_usd']['value'], Decimal('.01'))
        self.assertEqual(cost['paid_error_calls']['value'], 1)
        self.assertEqual(cost['paid_error_usd']['value'], Decimal('.01'))
        with self.assertRaises(ValueError):
            metrics.call_costs(calls + calls)

    def test_exhaustion_uses_unit_growth_not_authority_or_returned(self):
        batches = [{'level': level, 'status': 'ok', 'kind': 'generation', 'new_mechanism_ids': ids,
                    'authority_new_mechanism_ids': [], 'returned_idea_ids': ['returned'], 'fallback_ids': []}
                   for level, ids in [('round1', ['A', 'B']), ('round2', ['A', 'C']), ('pressure1', [])]]
        batches.insert(1, {**batches[0], 'kind': 'rewrite', 'new_mechanism_ids': ['A']})
        result = metrics.exhaustion(batches)
        self.assertEqual([result[k]['value'] for k in ['round1', 'round2', 'pressure1']], [2, 1, 0])
        self.assertIsNone(result['pressure2']['value'])
        batches[2]['status'] = 'not_evaluated'
        self.assertIsNone(metrics.exhaustion(batches)['round2']['value'])
        self.assertIsNone(metrics.exhaustion(None)['round1']['value'])

    def test_noise_accepts_arbitrary_finite_scores_not_machine_counts(self):
        self.assertEqual(metrics.noise_line({'a': [2, 5, 4], 'b': [1, 2, 1]})['value'], 3)
        self.assertEqual(metrics.noise_line({'a': [-2.5, 0, 1.5]})['value'], 4)
        self.assertIsNone(metrics.noise_line({'a': [1, 2], 'b': [2, 3, 4]})['value'])
        for bad in (True, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                metrics.noise_line({'a': [0, 1, bad]})

    def comparison_input(self, config, totals):
        rows = [idea(i + (100 if config == 'b' else 0), config=config) for i in range(4)]
        for i, row in enumerate(rows):
            row['call_key'] = f'{config}_{i // 2}'
            row['agent'] = 1
        rows[-1].update(returned=True, returned_from=rows[0]['idea_id'])
        # One invocation cannot mix generation and rewrite output.
        rows[-2].update(returned=True, returned_from=rows[0]['idea_id'])
        calls = [receipt(rows[0], totals[0]), receipt(rows[2], totals[1])]
        return {'ideas': rows, 'calls': calls, 'summary': {'complete': True, 'calls': len(calls), 'ideas': len(rows), 'failures': 0},
                'table': table_for(rows, [[r['idea_id']] for r in rows])}

    def test_both_comparison_controls_preserve_calls_and_output_order(self):
        a, b = self.comparison_input('a', [4, 7]), self.comparison_input('b', [5, 6])
        result = metrics.compare_units(a, b, seed=0, idea_count=3, token_cap=10)
        self.assertEqual(result['same_tokens']['units']['a']['config'], 'a')
        self.assertEqual(result['same_tokens']['units']['b']['config'], 'b')
        self.assertEqual(result['same_tokens']['token_difference'], -1)
        for side, source, used, unused in [('a', a, 4, 6), ('b', b, 5, 5)]:
            selected = result['same_ideas'][side]
            self.assertEqual(selected['idea_ids'], [r['idea_id'] for r in source['ideas'][:3]])
            self.assertEqual(selected['costs']['usd']['value'], Decimal('.02'))
            self.assertTrue(selected['partial_call'])
            token = result['same_tokens'][side]
            self.assertEqual(token['tokens'], used)
            self.assertEqual(token['unused'], unused)
            self.assertEqual(token['idea_ids'], [r['idea_id'] for r in source['ideas'][:2]])
        self.assertIn('非精确', result['same_tokens']['warning'])
        self.assertEqual(len(metrics.compare_units(a, b, seed=0, token_cap=11)['same_tokens']['a']['idea_ids']), 4)
        self.assertIsNone(metrics.compare_units(a, b, seed=0, idea_count=8)['same_ideas']['value'])
        low = metrics.compare_units(a, b, seed=0, token_cap=1)['same_tokens']
        self.assertIsNone(low['value'])
        self.assertEqual(low['a']['tokens'], 0)
        self.assertEqual(low['a']['unused'], 1)

    def test_token_control_accounts_for_paid_failure_and_rejects_missing_receipts(self):
        a, b = self.comparison_input('a', [4, 7]), self.comparison_input('b', [5, 6])
        a['calls'][0]['outcome'] = 'paid_error'
        a['ideas'] = a['ideas'][2:]
        a['summary']['ideas'] = len(a['ideas'])
        a['table'] = table_for(a['ideas'], [[r['idea_id']] for r in a['ideas']])
        result = metrics.compare_units(a, b, seed=0, token_cap=11)['same_tokens']
        self.assertEqual(result['a']['tokens'], 11)
        self.assertEqual(result['a']['call_keys'], ['a_0', 'a_1'])
        for changes in ({'tokens': {}}, {'outcome': 'uncertain'}):
            invalid = deepcopy(a)
            invalid['calls'][0].update(changes)
            self.assertIsNone(metrics.compare_units(invalid, b, seed=0)['same_tokens']['value'])
        a['calls'] = a['calls'][:1]
        self.assertIsNone(metrics.compare_units(a, b, seed=0)['same_tokens']['value'])

    def test_observed_empty_and_incomplete_units_do_not_share_status(self):
        complete = {'ideas': [], 'calls': [], 'table': table_for([], []),
                    'summary': {'complete': True, 'calls': 0, 'ideas': 0, 'failures': 0}}
        result = metrics.compute_unit(complete, exam_id='sample', config='test', rep=1)
        self.assertEqual(result['metrics']['unique_mechanisms']['value'], 0)
        self.assertEqual(result['metrics']['unique_mechanisms']['status'], 'ok')
        complete['summary']['complete'] = False
        result = metrics.compute_unit(complete, exam_id='sample', config='test', rep=1)
        self.assertIsNone(result['metrics']['unique_mechanisms']['value'])
        self.assertEqual(result['metrics']['unique_mechanisms']['status'], 'not_evaluated')

    def test_text_metrics_keep_all_points_and_stable_edges_under_input_order(self):
        rows = [('b', 'cd'), ('z', 'ab'), ('a', 'ab')]
        forward = metrics.text_metrics([v for _, v in rows], ids=[k for k, _ in rows])
        backward = metrics.text_metrics([v for _, v in reversed(rows)], ids=[k for k, _ in reversed(rows)])
        self.assertEqual(forward['mst']['edges'], backward['mst']['edges'])
        many = metrics.text_metrics(['ab'] * 81)
        self.assertEqual(many['pair_similarity']['pairs'], 3240)
        self.assertEqual(many['mst']['edge_count'], 80)

    def test_same_ideas_does_not_erase_failure_cost_before_the_output_cutoff(self):
        a, b = self.comparison_input('a', [4, 7]), self.comparison_input('b', [5, 6])
        a['calls'][0]['outcome'] = 'paid_error'
        a['ideas'] = a['ideas'][2:]
        a['summary']['ideas'] = len(a['ideas'])
        a['table'] = table_for(a['ideas'], [[r['idea_id']] for r in a['ideas']])
        result = metrics.compare_units(a, b, seed=0, idea_count=1)['same_ideas']
        self.assertEqual(result['a']['call_keys'], ['a_0', 'a_1'])
        self.assertEqual(result['a']['costs']['usd']['value'], Decimal('.02'))
        self.assertEqual(result['a']['text_space'], 'joint_selected_corpus')
