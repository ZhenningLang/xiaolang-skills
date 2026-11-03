"""Actual CLI, partition, ledger and disk acceptance with synthetic recordings."""

from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evals.brainstorm import loop, metrics, replay
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetExceeded, BudgetLedger, dumps
from evals.brainstorm.config import validate_config
from evals.brainstorm.dedupe import fake_partition
from evals.brainstorm.judge import fake_judge
from evals.brainstorm.exam import load_exams
from evals.brainstorm.record import digest, read_file, read_rows, record_batch
from evals.brainstorm.tests.test_loop import response
from evals.brainstorm.tests.test_dedupe import idea
from evals.brainstorm.tests.test_metrics import receipt, table_for


class ReplayAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = validate_config({'name': 'downstream', 'default_model': 'm'})
        self.exam = {'id': 'sample', 'question': 'PRIVATE_QUESTION_DO_NOT_ECHO', 'constraints': ['Keep access'],
                     'planted': [], 'wrong_premise': None}
        self.all_rows = []
        for unit_index, (config, rep) in enumerate([('a', 1), ('b', 1), ('a', 2)]):
            rows = []
            for agent in range(1, 21):
                for stage, level in enumerate(['baseline', 'round1', 'round2', 'pressure1']):
                    row = idea(unit_index * 1000 + agent * 10 + stage, agent=agent, level=level,
                               config=config, rep=rep, batch=stage + 1, text=['base', 'ab', 'cd', 'ab'][stage])
                    if stage == 3:
                        row.update(returned=True, returned_from=rows[-1]['idea_id'])
                    rows.append(row)
            self.all_rows.extend(rows)
            directory = self.root / f'sample/{config}/rep-{rep}'
            directory.mkdir(parents=True)
            self.write(directory / 'ideas.jsonl', rows, lines=True)
            self.write(directory / 'calls.jsonl', [receipt(row) for row in rows], lines=True)
            self.write(directory / 'run_summary.json', {'complete': True, 'calls': len(rows), 'ideas': len(rows),
                       'failures': 0, 'guards': {
                'injection': {'status': 'not_applicable', 'violations': None},
                'constraint': {'status': 'not_evaluated', 'violations': None}}})
            batches = [
                {'level': 'baseline', 'status': 'ok', 'new_mechanism_ids': [], 'fallback_ids': []},
                {'level': 'round1', 'status': 'ok', 'new_mechanism_ids': ['m-002'], 'fallback_ids': []},
                {'level': 'round2', 'status': 'ok', 'new_mechanism_ids': ['m-003'], 'fallback_ids': []},
                {'level': 'pressure1', 'status': 'ok', 'new_mechanism_ids': [], 'fallback_ids': []}]
            self.write(directory / 'loop_batches.jsonl', batches, lines=True)
            state = {'complete': True, 'projection': batches}
            self.write(directory / 'loop_state.json', {'digest': digest(state), 'state': state})
        groups = [[r['idea_id'] for r in self.all_rows if r['text'] == text] for text in ['base', 'ab', 'cd']]
        self.write(self.root / 'sample/mechanisms.json', table_for(self.all_rows, groups))
        self.config_path = self.root / 'config.json'
        self.write(self.config_path, {'name': 'downstream', 'default_model': 'm'})
        self.exam_path = self.root / 'exam.json'
        self.write(self.exam_path, self.exam)
        self.ledger = BudgetLedger(self.root / 'ledger.fake.jsonl')

    def write(self, path, value, lines=False):
        path.write_text(''.join(dumps(row) + '\n' for row in value) if lines else dumps(value) + '\n')

    def source_snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in (self.root / 'sample').rglob('*')
                if path.is_file()} | {'exam.json': self.exam_path.read_bytes()}

    def run_replay(self, **options):
        return replay.run_replay(self.root, self.exam, config='a', rep=1, name='trial', seed=42,
            sizes=options.pop('sizes', [5, 10, 20]), dedupe_config=self.config, judge_config=self.config,
            ledger=options.pop('ledger', self.ledger),
            dedupe_backend=options.pop('dedupe_backend', FakeBackend(responses=fake_partition)),
            judge_backend=options.pop('judge_backend', FakeBackend(responses=fake_judge)), **options)

    def test_real_downstream_replay_never_generates_or_mutates_sources(self):
        before = self.source_snapshot()
        with patch('evals.brainstorm.record.record_batch', side_effect=AssertionError('generation forbidden')) as generate, \
             patch('evals.brainstorm.loop.run_loop', side_effect=AssertionError('generation forbidden')), \
             patch('evals.brainstorm.record.fake_generation', side_effect=AssertionError('generation forbidden')), \
             patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            result = self.run_replay()
        self.assertEqual(generate.call_count, 0)
        self.assertEqual(self.source_snapshot(), before)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['samples']['20']['metrics']['unique_mechanisms']['value'], 2)
        self.assertEqual(result['samples']['20']['guards']['constraint']['violations'], 0)
        self.assertEqual(result['samples']['20']['counts']['baseline'], 20)
        self.assertEqual(result['samples']['20']['counts']['produced'], 60)
        paid = self.ledger.path.read_bytes()
        self.run_replay(dedupe_backend=FakeBackend(responses=[]), judge_backend=FakeBackend(responses=[]))
        self.assertEqual(self.ledger.path.read_bytes(), paid)

    def test_name_identity_conflicts_and_unsafe_paths_fail_before_calls(self):
        self.run_replay(sizes=[5])
        paid = self.ledger.path.read_bytes()
        for changes in ({'seed': 43}, {'sizes': [10]},
                        {'dedupe_config': {**self.config, 'default_model': 'other', 'models': ['other']}}):
            args = dict(config='a', rep=1, name='trial', seed=42, sizes=[5], dedupe_config=self.config,
                        judge_config=self.config, ledger=self.ledger, dedupe_backend=FakeBackend(responses=[]),
                        judge_backend=FakeBackend(responses=[])) | changes
            with self.assertRaisesRegex(ValueError, 'identity'):
                replay.run_replay(self.root, self.exam, **args)
        for name in ['../sample', '/sample']:
            with self.assertRaises(ValueError):
                replay.run_replay(self.root, self.exam, config='a', rep=1, name=name, seed=42,
                    dedupe_config=self.config, judge_config=self.config, ledger=self.ledger)
        (self.root / 'replay/alias').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            replay.run_replay(self.root, self.exam, config='a', rep=1, name='alias', seed=42,
                dedupe_config=self.config, judge_config=self.config, ledger=self.ledger)
        self.assertEqual(self.ledger.path.read_bytes(), paid)

    def test_budget_stops_each_downstream_boundary_and_resume_does_not_rebill(self):
        for index, limit in enumerate(['.001', '.03', '.05']):
            ledger = BudgetLedger(self.root / f'budget_{index}.jsonl', limit)
            name = f'budget_{index}'
            options = dict(config='a', rep=1, name=name, seed=42, sizes=[20],
                           dedupe_config=self.config, judge_config=self.config)
            with self.assertRaises(BudgetExceeded):
                replay.run_replay(self.root, self.exam, **options, ledger=ledger)
            partial = json.loads((self.root / f'replay/{name}/result.json').read_text())
            self.assertEqual(partial['status'], 'incomplete')
            resumed = replay.run_replay(self.root, self.exam, **options,
                                        ledger=BudgetLedger(ledger.path, '1'))
            self.assertEqual(resumed['status'], 'complete')
            settled = [row for row in read_rows(ledger.path) if row['event'] == 'settle']
            self.assertEqual(len(settled), 5)
            self.assertEqual(len({r['call_key'] for r in settled}), 5)
            self.assertEqual(sum(r['usd'] for r in settled), Decimal('.05'))
            reference = replay.run_replay(self.root, self.exam, **{**options, 'name': f'reference_{index}'},
                                          ledger=BudgetLedger(self.root / f'reference_{index}.jsonl'))
            self.assertEqual(resumed['samples']['20']['metrics'], reference['samples']['20']['metrics'])
            self.assertEqual(resumed['samples']['20']['costs']['replay']['usd'],
                             reference['samples']['20']['costs']['replay']['usd'])

    def guarded_cli(self, *arguments):
        guard = self.root / 'guards'
        guard.mkdir(exist_ok=True)
        (guard / 'sitecustomize.py').write_text('''from unittest.mock import patch
targets = ["evals.brainstorm.backends.ClaudeCliBackend.call", "evals.brainstorm.backends.CommandBackend.call",
"socket.create_connection", "socket.socket.connect", "evals.brainstorm.record.record_batch",
"evals.brainstorm.loop.run_loop", "evals.brainstorm.record.fake_generation",
"evals.brainstorm.prompts.generation_prompt"]
patches = [patch(target, side_effect=AssertionError("FORBIDDEN_EXECUTION_SENTINEL")) for target in targets]
for active in patches:
    active.start()
''')
        environment = {**os.environ, 'PYTHONPATH': str(guard) + os.pathsep + str(Path.cwd()),
                       'PYTHONDONTWRITEBYTECODE': '1'}
        return subprocess.run([sys.executable, *map(str, arguments)], env=environment,
                              text=True, capture_output=True, timeout=45)

    def cli_replay_args(self, name='cli', limit=None):
        args = ['-m', 'evals.brainstorm', 'replay', '--run-dir', self.root, '--exam-id', 'sample',
                '--config-name', 'a', '--rep', '1', '--exam', self.exam_path, '--dedupe-config', self.config_path,
                '--judge-config', self.config_path, '--name', name, '--seed', '42', '--sizes', '20']
        return args + (['--limit', limit] if limit is not None else [])

    def test_cli_replay_metrics_and_source_conservation_with_live_sentinels(self):
        negative_controls = [
            'from evals.brainstorm.backends import ClaudeCliBackend; ClaudeCliBackend.__new__(ClaudeCliBackend).call()',
            'from evals.brainstorm.backends import CommandBackend; CommandBackend.__new__(CommandBackend).call()',
            'import socket; socket.create_connection(("127.0.0.1", 1))',
            'from evals.brainstorm.record import fake_generation; fake_generation()',
            'from evals.brainstorm.record import record_batch; record_batch()',
            'from evals.brainstorm.loop import run_loop; run_loop()']
        for code in negative_controls:
            control = self.guarded_cli('-c', code)
            self.assertNotEqual(control.returncode, 0)
            self.assertIn('FORBIDDEN_EXECUTION_SENTINEL', control.stderr)
        before = self.source_snapshot()
        result = self.guarded_cli(*self.cli_replay_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads((self.root / 'replay/cli/result.json').read_text())
        self.assertEqual(output['generation_calls'], 0)
        self.assertEqual(output['samples']['20']['metrics']['unique_mechanisms']['value'], 2)
        self.assertEqual(self.source_snapshot(), before)
        for config, rep in [('a', 1), ('b', 1), ('a', 2)]:
            args = ['-m', 'evals.brainstorm', 'metrics', '--run-dir', self.root, '--exam-id', 'sample',
                    '--config-name', config, '--rep', rep, '--exam', self.exam_path]
            if config == 'a' and rep == 1:
                args += ['--compare', 'b', '--idea-count', '3', '--token-cap', '10']
            computed = self.guarded_cli(*args)
            self.assertEqual(computed.returncode, 0, computed.stderr)
            for text in ['相同点子数', '相同 token', '非精确', '粗估', 'source_recording', 'shared_dedupe', 'replay',
                         '护栏', '枯竭', 'linkage']:
                self.assertIn(text, computed.stdout)
        disk = json.loads((self.root / 'metrics.json').read_text())
        a = disk['exams']['sample']['a']
        self.assertEqual(set(a), {'1', '2'})
        self.assertEqual(a['1']['metrics']['unique_mechanisms']['value'], 2)
        self.assertEqual(a['1']['metrics']['unique_ratio']['value'], 2 / 60)
        self.assertEqual(a['1']['metrics']['outside_baseline_ratio']['value'], 1)
        self.assertEqual(a['1']['exhaustion']['round1']['value'], 1)
        self.assertEqual(a['1']['comparisons']['same_ideas']['value'], {'a': 3, 'b': 3})
        self.assertEqual(a['1']['comparisons']['same_tokens']['a']['tokens'], 10)
        for unit in [a['1'], a['2'], disk['exams']['sample']['b']['1']]:
            self.assertTrue(all('value' in entry and 'status' in entry for entry in unit['metrics'].values()))
            self.assertIsNone(unit['metrics']['human_pool_distance']['value'])
        self.assertEqual(self.source_snapshot(), before)

    def test_cli_budget_exit_three_and_safe_error_context(self):
        for index, limit in enumerate(['.001', '.03', '.05']):
            # Separate ledgers keep each stop at its intended downstream boundary.
            ledger = self.root / f'cli_budget_{index}.jsonl'
            name = f'budget_cli_{index}'
            stopped = self.guarded_cli(*self.cli_replay_args(name, limit), '--ledger', ledger)
            self.assertEqual(stopped.returncode, 3, stopped.stderr)
            self.assertTrue((self.root / f'replay/{name}/manifest.json').is_file())
            resumed = self.guarded_cli(*self.cli_replay_args(name, '1'), '--ledger', ledger)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
        failed = self.guarded_cli(*self.cli_replay_args('../sample'))
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('sample/a/rep-1', failed.stderr)
        self.assertNotIn(self.exam['question'], failed.stderr)

    def test_source_change_and_output_file_alias_are_rejected_without_new_costs(self):
        self.run_replay(sizes=[5])
        paid = self.ledger.path.read_bytes()
        source = self.root / 'sample/a/rep-1/ideas.jsonl'
        source.write_text(source.read_text().replace('{', '{ ', 1))
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.run_replay(sizes=[5])
        self.assertEqual(self.ledger.path.read_bytes(), paid)
        (self.root / 'replay/unsafe').mkdir()
        (self.root / 'replay/unsafe/manifest.json').symlink_to(self.exam_path)
        with self.assertRaises(ValueError):
            replay.run_replay(self.root, self.exam, config='a', rep=1, name='unsafe', seed=42,
                dedupe_config=self.config, judge_config=self.config, ledger=self.ledger)
        self.assertEqual(self.ledger.path.read_bytes(), paid)

    def test_crash_after_downstream_settlement_recovers_all_receipts_once(self):
        with patch.object(replay, 'save_metrics', side_effect=OSError('simulated disk failure')):
            with self.assertRaisesRegex(OSError, 'simulated disk failure'):
                self.run_replay(sizes=[20])
        result = self.run_replay(sizes=[20])
        self.assertEqual(result['status'], 'complete')
        settled = [event for event in read_rows(self.ledger.path) if event['event'] == 'settle']
        self.assertEqual(len(settled), 5)
        self.assertEqual(len({event['call_key'] for event in settled}), 5)

    def test_fallback_is_missing_mechanism_evidence_not_a_new_mechanism_score(self):
        result = self.run_replay(sizes=[5], dedupe_backend=FakeBackend(responses=['not JSON']))
        unit = result['samples']['5']
        self.assertEqual(unit['counts']['total'], 20)
        self.assertEqual(unit['metrics']['unique_mechanisms']['status'], 'not_evaluated')
        self.assertIsNone(unit['metrics']['unique_mechanisms']['value'])
        self.assertEqual(unit['costs']['replay']['usd']['value'], Decimal('.04'))

    def loop_source(self, name, *, limit='50', backend=None, levels=None, max_batches=1):
        root = self.root / 'actual'
        exam = load_exams(Path(__file__).resolve().parents[1] / 'exams/samples/open')[0]
        config = validate_config({'name': name, 'default_model': 'm', 'agents': 1, 'max_batches': max_batches,
                                  'levels': levels or ['round1', 'round2'], 'method_mode': 'none'})
        def generated(prompt, *_):
            level = prompt.split('Generation level: ', 1)[1].split('.', 1)[0]
            return response(level)
        ledger = BudgetLedger(root / 'ledger.fake.jsonl', limit)
        result = loop.run_loop(exam, config, root, seed=19, backend=backend or FakeBackend(responses=generated),
                               ledger=ledger, dedupe_config=self.config, dedupe_backend=FakeBackend(responses=fake_partition))
        return root, exam, result

    def actual_replay(self, root, exam, config, **options):
        return replay.run_replay(root, exam, config=config, rep=1, name='actual_replay', seed=42, sizes=[1],
            dedupe_config=self.config, judge_config=self.config, ledger=BudgetLedger(root / 'ledger.fake.jsonl'), **options)

    def test_real_loop_neighbor_generation_frontier_is_local_and_resumable(self):
        root, exam, _ = self.loop_source('healthy')
        before = metrics.compute_unit(metrics.load_unit(root, exam['id'], 'healthy', 1),
                                      exam_id=exam['id'], config='healthy', rep=1)
        self.assertEqual(before['metrics']['unique_mechanisms']['value'], 2)
        with self.assertRaises(BudgetExceeded):
            self.loop_source('stopped', limit='.12')
        source = {str(p): p.read_bytes() for p in (root / exam['id']).rglob('*') if p.is_file()}
        for name in ['healthy', 'stopped']:
            command = ['-m', 'evals.brainstorm', 'metrics', '--run-dir', root, '--exam-id', exam['id'],
                       '--config-name', name]
            result = self.guarded_cli(*command)
            self.assertEqual(result.returncode, 0, result.stderr)
        disk = json.loads((root / 'metrics.json').read_text())['exams'][exam['id']]
        self.assertEqual(disk['healthy']['1']['metrics'], before['metrics'])
        self.assertIsNone(disk['stopped']['1']['metrics']['unique_mechanisms']['value'])
        self.assertEqual(self.actual_replay(root, exam, 'healthy')['status'], 'complete')
        self.assertEqual({str(p): p.read_bytes() for p in (root / exam['id']).rglob('*') if p.is_file()}, source)
        self.loop_source('stopped')
        after = metrics.compute_unit(metrics.load_unit(root, exam['id'], 'stopped', 1),
                                     exam_id=exam['id'], config='stopped', rep=1)
        self.assertEqual(after['metrics']['unique_mechanisms']['value'], 2)
        self.assertEqual(after['exhaustion']['baseline']['status'], 'not_evaluated')

    def test_real_loop_finalize_crash_blocks_full_metrics_comparison_and_new_replay_fees(self):
        self.loop_source('ready')
        save = loop.save_state
        crashed = []
        def interrupt(path, state):
            save(path, state)
            if (state['batches'] and state['batches'][-1]['level'] == 'round1'
                    and state['batches'][-1]['stage'] == 'finalize' and not crashed):
                crashed.append(True)
                raise OSError('crash before loop finalize')
        with patch.object(loop, 'save_state', side_effect=interrupt), self.assertRaises(OSError):
            self.loop_source('window')
        root = self.root / 'actual'
        exam = load_exams(Path(__file__).resolve().parents[1] / 'exams/samples/open')[0]
        pending = metrics.load_unit(root, exam['id'], 'window', 1)
        self.assertTrue(pending['summary']['complete'])
        self.assertFalse(pending['loop_complete'])
        result = metrics.compute_unit(pending, exam_id=exam['id'], config='window', rep=1)
        self.assertTrue(all(entry['status'] == 'not_evaluated' for entry in result['metrics'].values()))
        ready = metrics.load_unit(root, exam['id'], 'ready', 1)
        self.assertEqual(metrics.compare_units(pending, ready, seed=0)['same_ideas']['status'], 'not_evaluated')
        ledger_bytes = (root / 'ledger.fake.jsonl').read_bytes()
        backend = FakeBackend(responses=fake_partition)
        with self.assertRaisesRegex(ValueError, 'loop incomplete'):
            self.actual_replay(root, exam, 'window', dedupe_backend=backend)
        self.assertEqual(backend.calls, 0)
        self.assertEqual((root / 'ledger.fake.jsonl').read_bytes(), ledger_bytes)
        self.loop_source('window')
        self.assertEqual(self.actual_replay(root, exam, 'window')['status'], 'complete')

    def test_standalone_recording_and_legitimate_unrun_loop_levels_remain_supported(self):
        root, exam, _ = self.loop_source('methods_off', levels=['pressure4'])
        data = metrics.load_unit(root, exam['id'], 'methods_off', 1)
        self.assertEqual(data['batches'][-1]['status'], 'not_evaluated')
        self.assertEqual(metrics.unit_completion(data)['status'], 'ok')
        config = validate_config({'name': 'standalone', 'default_model': 'm', 'agents': 1})
        record_batch(exam, config, root, seed=19, backend=FakeBackend(responses=[response('ungrouped')]),
                     ledger=BudgetLedger(root / 'ledger.fake.jsonl'))
        data = metrics.load_unit(root, exam['id'], 'standalone', 1)
        result = metrics.compute_unit(data, exam_id=exam['id'], config='standalone', rep=1)
        self.assertEqual(result['completion']['status'], 'ok')
        self.assertIsNone(result['metrics']['unique_mechanisms']['value'])
        self.assertEqual(result['costs']['source_recording']['usd']['value'], Decimal('.01'))

    def test_numeric_backend_config_cli_budget_resume_keeps_identity_and_costs(self):
        self.write(self.config_path, {'name': 'downstream', 'default_model': 'm',
                   'backend': {'kind': 'fake', 'usd': .01, 'reserve_usd': .02}})
        stopped = self.guarded_cli(*self.cli_replay_args('numeric_config', '.03'))
        self.assertEqual(stopped.returncode, 3, stopped.stderr)
        resumed = self.guarded_cli(*self.cli_replay_args('numeric_config', '1'))
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        ledger_path = self.root / 'ledger.fake.jsonl'
        settled = [row for row in read_rows(ledger_path) if row['event'] == 'settle']
        self.assertEqual(len(settled), 5)
        self.assertEqual(sum(row['usd'] for row in settled), Decimal('.05'))
        before = ledger_path.read_bytes()
        self.assertEqual(self.guarded_cli(*self.cli_replay_args('numeric_config', '1')).returncode, 0)
        self.assertEqual(ledger_path.read_bytes(), before)

    def test_invalid_exam_is_rejected_before_any_downstream_charge(self):
        invalid = {key: value for key, value in self.exam.items() if key != 'constraints'}
        before = self.ledger.path.read_bytes() if self.ledger.path.exists() else b''
        backend = FakeBackend(responses=fake_partition)
        with self.assertRaisesRegex(ValueError, 'constraints'):
            replay.run_replay(self.root, invalid, config='a', rep=1, name='invalid_exam', seed=42,
                sizes=[5], dedupe_config=self.config, judge_config=self.config, ledger=self.ledger, dedupe_backend=backend)
        self.assertEqual(backend.calls, 0)
        self.assertEqual(self.ledger.path.read_bytes() if self.ledger.path.exists() else b'', before)

    def test_replay_reports_baseline_constraints_separately_from_produced_ideas(self):
        def verdict(prompt, model, maximum):
            data = json.loads(fake_judge(prompt, model, maximum))
            if 'verdicts' in data:
                data['verdicts'][0].update(violation=True, reason='baseline only failure')
            return json.dumps(data)
        result = self.run_replay(sizes=[5], judge_backend=FakeBackend(responses=verdict))['samples']['5']
        self.assertEqual(result['guards']['constraint']['violations'], 0)
        self.assertEqual(result['guards']['constraint']['baseline']['violations'], 1)
        self.assertEqual(result['guard_scopes']['injection'], 'original_recording_unit')

    def test_actual_recording_receipt_file_gaps_are_local_missing_metrics_not_zero(self):
        root, exam, _ = self.loop_source('receipt_gaps')
        path = root / exam['id'] / 'receipt_gaps/rep-1/calls.jsonl'
        original = path.read_bytes()
        calls = read_rows(path)
        variants = [('absent', None, 'usd'), ('empty', [], 'usd'), ('partial', calls[:1], 'usd'),
                    ('missing_usd', [{**row, 'usd': None} for row in calls], 'usd'),
                    ('missing_tokens', [{**row, 'tokens': {}} for row in calls], 'tokens')]
        try:
            for name, rows, key in variants:
                with self.subTest(name=name):
                    if rows is None:
                        path.unlink()
                    else:
                        self.write(path, rows, lines=True)
                    result = metrics.compute_unit(metrics.load_unit(root, exam['id'], 'receipt_gaps', 1),
                        exam_id=exam['id'], config='receipt_gaps', rep=1)
                    self.assertIsNone(result['costs']['source_recording'][key]['value'])
                    self.assertEqual(result['metrics']['unique_mechanisms']['value'], 2)
                    self.assertIn('not_evaluated', metrics.summary_text(result))
        finally:
            path.write_bytes(original)
        shared = root / exam['id'] / 'dedupe/calls.jsonl'
        original_shared = shared.read_bytes()
        try:
            for rows in [[], read_rows(shared)[:1]]:
                self.write(shared, rows, lines=True)
                result = metrics.compute_unit(metrics.load_unit(root, exam['id'], 'receipt_gaps', 1),
                    exam_id=exam['id'], config='receipt_gaps', rep=1)
                self.assertIsNone(result['costs']['shared_dedupe']['usd']['value'])
                self.assertEqual(result['metrics']['unique_mechanisms']['value'], 2)
        finally:
            shared.write_bytes(original_shared)

    def test_missing_same_level_batch_invalidates_growth_until_projection_restored(self):
        backend = FakeBackend()
        backend.responses = lambda prompt, *_: response('baseline' if 'Generation level: baseline.' in prompt
                                                       else f'new-{backend.calls}')
        root, exam, _ = self.loop_source('projection_gap', backend=backend, levels=['round1'], max_batches=2)
        path = root / exam['id'] / 'projection_gap/rep-1/loop_batches.jsonl'
        original = path.read_bytes()
        rows = read_rows(path)
        self.assertEqual([row['new_mechanisms'] for row in rows[-2:]], [1, 1])
        try:
            self.write(path, rows[:-1], lines=True)
            data = metrics.load_unit(root, exam['id'], 'projection_gap', 1)
            result = metrics.compute_unit(data, exam_id=exam['id'], config='projection_gap', rep=1)
            self.assertFalse(data['loop_projection_current'])
            self.assertEqual(result['exhaustion']['round1']['status'], 'not_evaluated')
            self.assertIsNone(result['exhaustion']['round1']['value'])
            command = ['-m', 'evals.brainstorm', 'metrics', '--run-dir', root, '--exam-id', exam['id'],
                       '--config-name', 'projection_gap']
            cli = self.guarded_cli(*command)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            growth = json.loads(cli.stdout.split('枯竭: ', 1)[1].splitlines()[0])['round1']
            self.assertIsNone(growth['value'])
            stored = read_file(root / 'metrics.json')['exams'][exam['id']]['projection_gap']['1']
            self.assertEqual(stored['exhaustion']['round1']['status'], 'not_evaluated')
        finally:
            path.write_bytes(original)
        restored = metrics.compute_unit(metrics.load_unit(root, exam['id'], 'projection_gap', 1),
                                        exam_id=exam['id'], config='projection_gap', rep=1)
        self.assertEqual(restored['exhaustion']['round1']['value'], 2)

    def test_pending_same_level_projection_preserves_closed_earlier_level(self):
        backend = FakeBackend()
        backend.responses = lambda prompt, *_: response('baseline' if 'Generation level: baseline.' in prompt
                                                       else f'new-{backend.calls}')
        write = loop.atomic_write
        def fail_projection(path, text):
            if Path(path).name == 'loop_batches.jsonl':
                rows = [json.loads(line) for line in text.splitlines()]
                round2 = [row for row in rows if row['level'] == 'round2']
                if len(round2) == 2 and round2[-1]['status'] == 'not_evaluated':
                    raise OSError('public projection write failed after state committed')
            return write(path, text)
        with patch.object(loop, 'atomic_write', side_effect=fail_projection), self.assertRaises(OSError):
            self.loop_source('pending_projection', backend=backend, max_batches=2)
        root = self.root / 'actual'
        exam = load_exams(Path(__file__).resolve().parents[1] / 'exams/samples/open')[0]
        data = metrics.load_unit(root, exam['id'], 'pending_projection', 1)
        self.assertFalse(data['loop_complete'])
        self.assertFalse(data['loop_projection_current'])
        result = metrics.compute_unit(data, exam_id=exam['id'], config='pending_projection', rep=1)
        self.assertEqual(result['exhaustion']['round1']['value'], 2)
        self.assertEqual(result['exhaustion']['round2']['status'], 'not_evaluated')
        self.assertIsNone(result['exhaustion']['round2']['value'])

    def test_failed_standalone_recordings_never_certify_complete_populations_or_replay(self):
        root, exam, _ = self.loop_source('healthy_control')
        healthy = metrics.load_unit(root, exam['id'], 'healthy_control', 1)
        for kind in ['all_paid', 'some_paid', 'parse_failed', 'recovered_parse']:
            config = validate_config({'name': kind, 'default_model': 'm', 'agents': 3})
            backend = FakeBackend(failure='paid_error' if kind == 'all_paid' else None)
            def generated(prompt, *_):
                if kind == 'some_paid':
                    backend.failure = 'paid_error' if backend.calls == 2 else None
                if kind == 'parse_failed' and backend.calls >= 3:
                    return 'not JSON'
                if kind == 'recovered_parse' and backend.calls == 1:
                    return 'not JSON'
                return response('observed')
            backend.responses = generated
            ledger = BudgetLedger(root / 'ledger.fake.jsonl')
            record_batch(exam, config, root, seed=19, backend=backend, ledger=ledger)
            source = root / exam['id'] / kind / 'rep-1'
            rows = read_rows(source / 'ideas.jsonl')
            from evals.brainstorm.dedupe import deduplicate
            deduplicate(rows, root, exam_id=exam['id'], seed=19, backend=FakeBackend(responses=fake_partition),
                        ledger=ledger, model='m')
            data = metrics.load_unit(root, exam['id'], kind, 1)
            self.assertTrue(data['summary']['complete'])
            result = metrics.compute_unit(data, exam_id=exam['id'], config=kind, rep=1)
            cost = result['costs']['source_recording']
            self.assertEqual(cost['usd']['value'], Decimal('.04' if kind in ['parse_failed', 'recovered_parse'] else '.03'))
            if kind == 'recovered_parse':
                self.assertEqual(data['summary']['failures'], 0)
                self.assertEqual(result['completion']['status'], 'ok')
                self.assertEqual(result['metrics']['unique_mechanisms']['value'], 1)
                continue
            self.assertGreater(data['summary']['failures'], 0)
            self.assertEqual(result['completion']['status'], 'not_evaluated')
            self.assertTrue(all(item['status'] == 'not_evaluated' for item in result['metrics'].values()))
            self.assertEqual(cost['paid_error_calls']['value'], 3 if kind == 'all_paid' else 1 if kind == 'some_paid' else 0)
            for mode in metrics.compare_units(data, healthy, seed=0).values():
                self.assertEqual(mode['status'], 'not_evaluated')
                if kind != 'all_paid':
                    self.assertIn('a', mode)
                    if 'tokens' in mode['a']:
                        self.assertGreater(mode['a']['tokens'], 0)
            accounting = metrics.compare_units(data, healthy, seed=0, token_cap=1000000)['same_tokens']
            self.assertEqual(accounting['status'], 'not_evaluated')
            self.assertEqual(accounting['a']['tokens'], sum(row['tokens'][field] for row in data['calls']
                for field in ('input', 'cache_write', 'output')))
            self.assertEqual(accounting['a']['costs']['usd']['value'], cost['usd']['value'])
            before = ledger.path.read_bytes()
            downstream = FakeBackend(responses=fake_partition)
            with self.assertRaisesRegex(ValueError, 'population incomplete'):
                self.actual_replay(root, exam, kind, dedupe_backend=downstream)
            self.assertEqual(downstream.calls, 0)
            self.assertEqual(ledger.path.read_bytes(), before)
            cli = self.guarded_cli('-m', 'evals.brainstorm', 'metrics', '--run-dir', root, '--exam-id', exam['id'],
                                   '--config-name', kind)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            stored = read_file(root / 'metrics.json')['exams'][exam['id']][kind]['1']
            self.assertIsNone(stored['metrics']['unique_mechanisms']['value'])
            self.assertNotIn('unique_mechanisms | 0 | ok', cli.stdout)
