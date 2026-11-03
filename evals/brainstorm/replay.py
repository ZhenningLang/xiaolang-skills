"""Cold downstream replay of recorded agent populations; never generate ideas."""

from pathlib import Path
import random
import sys

from .backends import NotStarted
from .budget import BudgetLedger, LedgerError, atomic_write, cli_boundary, default_ledger_path
from .config import load_config, positive_int, validate_config
from .dedupe import deduplicate, fake_partition, stage_path
from .exam import safe_segment
from .judge import check_exam, evaluate, fake_judge, safe_output, self_evaluate, stage_backend
from .metrics import (compute_unit, dumps, load_unit, missing, observed,
                      receipt_evidence, save_metrics, summary_text, unit_completion, unit_exhaustion, unit_rows)
from .record import read_file, read_rows, recording_lock


def sample_agents(ideas, *, exam_id, config, rep, seed, sizes=(5, 10, 20)):
    if type(seed) is not int or not sizes or len(set(sizes)) != len(sizes):
        raise ValueError('Replay requires integer seed and unique nonempty sizes')
    for size in sizes:
        positive_int(size, 'agent sample size')
    rows = unit_rows(ideas, exam_id, config, rep)
    agents = sorted({row['agent'] for row in rows})
    random.Random(seed).shuffle(agents)
    result = {}
    for size in sizes:
        if size > len(agents):
            result[str(size)] = missing('Insufficient recorded agents; no replacement sampling',
                                        requested=size, available=len(agents))
        else:
            selected = agents[:size]
            result[str(size)] = observed(size, requested=size, available=len(agents), agents=selected,
                idea_ids=[row['idea_id'] for row in rows if row['agent'] in selected],
                source_line_indices=[i for i, row in enumerate(rows) if row['agent'] in selected])
    return result


def run_replay(run_dir, exam, *, config, rep, name, seed, dedupe_config, judge_config, ledger,
               sizes=(5, 10, 20), dedupe_backend=None, judge_backend=None):
    """Read recorded units and spend only on isolated downstream classification/evaluation."""
    root = Path(run_dir).resolve()
    check_exam(exam)
    safe_segment(name)
    dedupe_config, judge_config = validate_config(dedupe_config), validate_config(judge_config)
    dedupe_backend = dedupe_backend if dedupe_backend is not None else stage_backend(dedupe_config, fake_partition)
    judge_backend = judge_backend if judge_backend is not None else stage_backend(judge_config, fake_judge)
    if ((dedupe_backend.name == 'fake') != (judge_backend.name == 'fake') or
            dedupe_backend.name != dedupe_config['backend']['kind'] or judge_backend.name != judge_config['backend']['kind']):
        raise ValueError('Replay backend/config identity mismatch or mixed fake/real billing')
    data = load_unit(root, exam['id'], config, rep)
    required = (unit_completion(data), receipt_evidence(data))
    if any(result['status'] != 'ok' for result in required):
        reason = next(result['reason'] for result in required if result['status'] != 'ok')
        raise ValueError(f'Replay requires a complete source unit: {reason}')
    samples = sample_agents(data['ideas'], exam_id=exam['id'], config=config, rep=rep, seed=seed, sizes=sizes)
    base = stage_path(root, exam['id'], name).parent
    safe_output(base, ('manifest.json', 'result.json', 'record.lock'))
    partitions = {str(size): stage_path(root, exam['id'], name + '__n' + str(size)) for size in sizes}
    # Preflight every owned path, including existing nested evaluation outputs, before any billing.
    for directory in [base, *partitions.values()]:
        if directory.exists() and any(path.is_symlink() for path in directory.rglob('*')):
            raise ValueError('Replay output subtree contains a symlink alias')
        safe_output(directory, ('ideas.jsonl', 'metrics.json', 'metrics_state', 'judges'))
    identity = {'schema_version': 1, 'exam': exam, 'unit': [exam['id'], config, rep], 'seed': seed,
                'sizes': list(sizes), 'source_hashes': data['source_hashes'], 'samples': samples,
                'order_policy': 'sorted_agents_seeded_permutation_original_recording_lines',
                'dedupe_config': {k: v for k, v in dedupe_config.items() if k != 'budget_usd'},
                'judge_config': {k: v for k, v in judge_config.items() if k != 'budget_usd'},
                'ledger': str(ledger.path)}
    with recording_lock(base):
        manifest_path = base / 'manifest.json'
        if manifest_path.exists():
            if dumps(read_file(manifest_path)) != dumps(identity):
                raise ValueError('Replay name input identity conflict; use a fresh name')
        elif any(path.exists() for path in partitions.values()) or (base / 'result.json').exists():
            raise ValueError('Replay requires cold partitions when manifest is absent')
        atomic_write(manifest_path, dumps(identity) + '\n')
        report = {'schema_version': 1, 'status': 'incomplete', 'authoritative': False,
                  'unit': {'exam_id': exam['id'], 'config': config, 'rep': rep},
                  'name': name, 'seed': seed, 'samples': {}, 'generation_calls': 0,
                  'warning': '历史 agent 抽样不是改变生成策略的反事实实验；评委不是作者主分'}
        def save():
            atomic_write(base / 'result.json', dumps(report) + '\n')
        save()
        for size, sample in samples.items():
            if sample['status'] != 'ok':
                report['samples'][size] = sample
                save()
                continue
            path = partitions[size]
            selected = set(sample['idea_ids'])
            rows = [row for row in data['ideas'] if row['idea_id'] in selected]
            path.mkdir(parents=True, exist_ok=True)
            atomic_write(path / 'ideas.jsonl', ''.join(dumps(row) + '\n' for row in rows))
            print(f"Replay {exam['id']}/{config}/rep-{rep} agents={size} stage=dedupe", file=sys.stderr)
            try:
                grouped = deduplicate(rows, root, exam_id=exam['id'], seed=seed, backend=dedupe_backend,
                    ledger=ledger, model=dedupe_config['default_model'],
                    max_output_tokens=dedupe_config['max_output_tokens'], replay=name + '__n' + size)
                print(f"Replay {exam['id']}/{config}/rep-{rep} agents={size} stage=judge", file=sys.stderr)
                ranking = evaluate(rows, exam, path / 'judges/rank', backend=judge_backend, ledger=ledger,
                    model=judge_config['default_model'], max_output_tokens=judge_config['max_output_tokens'])
                self_ranking = self_evaluate(rows, exam, path / 'judges/self', backend=judge_backend, ledger=ledger,
                    allowed_models=judge_config['models'], max_output_tokens=judge_config['max_output_tokens'])
                print(f"Replay {exam['id']}/{config}/rep-{rep} agents={size} stage=constraint", file=sys.stderr)
                constraint = evaluate(rows, exam, path / 'judges/constraint', backend=judge_backend, ledger=ledger,
                    model=judge_config['default_model'], mode='constraint',
                    max_output_tokens=judge_config['max_output_tokens'])
            except (LedgerError, NotStarted, ValueError, OSError) as exc:
                report['stage_error'] = {'sample': size, 'reason': type(exc).__name__}
                save()
                if isinstance(exc, NotStarted):
                    raise ValueError(f"Replay {exam['id']}/{config}/rep-{rep} provider not started") from exc
                raise
            downstream_calls = [*read_rows(grouped['calls_path']),
                *(row for source in sorted((path / 'judges').rglob('calls.jsonl')) for row in read_rows(source))]
            report['generation_calls'] += sum(call['purpose'] in ('generate', 'rewrite') for call in downstream_calls)
            baseline_ids = {row['idea_id'] for row in rows if row['level'] == 'baseline'}
            if constraint['status'] == 'checked':
                constraint = {**constraint, 'violations': sum(row['violation'] for row in constraint['verdicts']
                    if row['idea_id'] not in baseline_ids), 'violation_scope': 'sample_nonbaseline_ideas',
                    'baseline': {'status': 'checked', 'violations': sum(row['violation'] for row in constraint['verdicts']
                        if row['idea_id'] in baseline_ids)}}
            source_calls = [call for call in data['calls'] if call['agent'] in sample['agents']]
            subset = {**data, 'ideas': rows, 'calls': source_calls, 'table': grouped['table'],
                      'summary': {**data['summary'], 'calls': len(source_calls), 'ideas': len(rows)},
                      'fallback_ids': grouped['fallback_ids'], 'batches': None}
            result = compute_unit(subset, exam_id=exam['id'], config=config, rep=rep, exam=exam,
                constraint=constraint, scope='replay_sample_non_authoritative', replay_calls=downstream_calls)
            result.update(sample=sample, ranking=ranking, self_ranking=self_ranking,
                          produced_ranking=[entry for entry in ranking.get('ranking', []) if entry['idea_id'] not in baseline_ids],
                          baseline_ranking=[entry for entry in ranking.get('ranking', []) if entry['idea_id'] in baseline_ids],
                          ranking_scope='baseline_and_produced_reference_only',
                          guard_scopes={'injection': 'original_recording_unit', 'constraint': 'sample_nonbaseline_ideas'},
                          source_cost_scope='selected_recorded_agents', baseline_scope='sample_unit',
                          source_exhaustion=unit_exhaustion(data),
                          source_exhaustion_scope='not_recomputed_from_final_partition')
            save_metrics(path, result)
            report['samples'][size] = result
            save()
        report['status'] = 'complete'
        save()
        return report


def run_cli(args):
    dedupe_config, judge_config = load_config(args.dedupe_config), load_config(args.judge_config)
    backend = stage_backend(dedupe_config, fake_partition)
    ledger = BudgetLedger(args.ledger or default_ledger_path(backend.name, args.run_dir),
                          args.limit if args.limit is not None else dedupe_config['budget_usd'])
    label = f'replay {args.exam_id}/{args.config_name}/rep-{args.rep}'
    exam = read_file(args.exam)
    if exam.get('id') != args.exam_id:
        raise ValueError(f'{label}: exam evidence identity mismatch')
    try:
        result = run_replay(args.run_dir, exam, config=args.config_name, rep=args.rep, name=args.name,
            seed=args.seed, sizes=args.sizes, dedupe_config=dedupe_config, judge_config=judge_config,
            ledger=ledger, dedupe_backend=backend)
    except (ValueError, OSError) as exc:
        raise ValueError(f'{label}: {exc}') from exc
    print(f"回放 {result['status']}，生成调用={result['generation_calls']}，输出仅供下游比较。")
    for size, entry in result['samples'].items():
        print(f'抽样 agent 数: {size}')
        print(summary_text(entry) if 'metrics' in entry else dumps(entry))


def register(subparsers):
    parser = subparsers.add_parser('replay', help='Sample recorded agents and rerun only downstream stages')
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--exam-id', required=True)
    parser.add_argument('--config-name', required=True)
    parser.add_argument('--rep', type=int, default=1)
    parser.add_argument('--exam', type=Path, required=True)
    parser.add_argument('--dedupe-config', type=Path, required=True)
    parser.add_argument('--judge-config', type=Path, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--sizes', nargs='+', type=int, default=[5, 10, 20])
    parser.add_argument('--ledger', type=Path)
    parser.add_argument('--limit')
    parser.set_defaults(func=lambda args: cli_boundary(run_cli, args))
