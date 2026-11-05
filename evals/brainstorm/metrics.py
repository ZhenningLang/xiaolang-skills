"""Evidence-aware machine proxies; these are not the author's primary score."""

from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal
from itertools import combinations
import hashlib
import json
import math
from pathlib import Path
import random
import unicodedata

from .backends import amount, exact_sum, token_counts
from .budget import atomic_write, cli_boundary
from .config import LEVELS, positive_int
from .dedupe import checked_ideas, validate_table
from .exam import safe_segment
from .record import read_file, read_rows, recording_lock


def dumps(value):
    """Canonical finite JSON numbers for metrics, not the unsigned USD codec."""
    if isinstance(value, (Decimal, float)):
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite():
            raise ValueError('Metric numbers must be finite')
        if number.is_zero():
            return '0'
        sign, digits, exponent = number.as_tuple()
        original = ''.join(map(str, digits))
        coefficient = original.rstrip('0')
        exponent += len(original) - len(coefficient)
        # Keep exponents compact; fixed-point formatting can allocate unbounded zeroes.
        return ('-' if sign else '') + coefficient + (f'e{exponent}' if exponent else '')
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(key) + ':' + dumps(value[key]) for key in sorted(value)) + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(dumps(item) for item in value) + ']'
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def observed(value, **details):
    return {'value': value, 'status': 'ok', **details}


def missing(reason, **details):
    return {'value': None, 'status': 'not_evaluated', 'reason': reason, **details}


def unit_rows(ideas, exam_id, config, rep):
    checked_ideas(ideas, exam_id)
    safe_segment(config)
    positive_int(rep, 'rep')
    return [row for row in ideas if (row['config'], row['rep']) == (config, rep)]


def partition_members(table, ideas, exam_id):
    """Validate the recorded partition, not the ungrouped recording frontier."""
    if not isinstance(table, dict) or not isinstance(table.get('mechanisms'), list):
        raise ValueError('Invalid mechanism table')
    members = []
    for group in table['mechanisms']:
        if not isinstance(group, dict) or not isinstance(group.get('idea_ids'), list):
            raise ValueError('Invalid mechanism members')
        members.extend(group['idea_ids'])
    if any(not isinstance(key, str) or key not in ideas for key in members):
        raise ValueError('Mechanism references unknown Idea')
    grouped = {key: ideas[key] for key in set(members)}
    validate_table(table, grouped, exam_id)
    return set(members)


def recording_completion(data):
    """Terminal recording/loop evidence is independent of generation success."""
    summary = data.get('summary', {})
    if not data.get('ideas_present', True) or summary.get('complete') is not True:
        return missing('Recording completion evidence missing or incomplete')
    if type(summary.get('ideas')) is not int or summary['ideas'] != len(data['ideas']):
        return missing('Recording summary/ideas count missing or mismatched')
    if type(summary.get('calls')) is not int or summary['calls'] < 0:
        return missing('Recording summary/calls count missing')
    if data.get('loop_present', False):
        if data.get('loop_complete') is not True:
            return missing('Generation loop incomplete; recorded batches are only an observed prefix')
        if data.get('loop_projection_current') is not True:
            return missing('Generation loop public projection has not caught up with committed state')
        return observed(True, scope='completed_generation_loop')
    if data.get('batches') is not None:
        return missing('Loop batches exist without authoritative loop completion evidence')
    return observed(True, scope='completed_standalone_recording')


def unit_completion(data):
    recorded = recording_completion(data)
    if recorded['status'] != 'ok':
        return recorded
    failures = data.get('summary', {}).get('failures')
    if type(failures) is not int or failures < 0:
        return missing('Recording success evidence missing; population incomplete')
    if failures or any(call.get('purpose') in ('generate', 'rewrite') and call.get('outcome') != 'ok'
                       for call in data['calls']):
        return missing('Recording has failed generation/parse outcomes; population incomplete')
    return recorded


def receipt_evidence(data):
    if not data.get('calls_present', True):
        return missing('Call receipt file missing')
    reason = linked_calls(data['ideas'], data['calls'])
    if reason:
        return missing(reason)
    count = data.get('summary', {}).get('calls')
    if type(count) is not int or count != len(data['calls']):
        return missing('Call receipt count disagrees with recording summary')
    return observed(True)


def mechanism_metrics(ideas, table, *, exam_id, config, rep, complete=True, fallback_ids=()):
    rows = unit_rows(ideas, exam_id, config, rep)
    produced = [row for row in rows if row['level'] != 'baseline']
    keys = ('unique_mechanisms', 'unique_ratio', 'outside_baseline_ratio', 'agent_overlap',
            'chao1', 'remaining_space')
    members = partition_members(table, {row['idea_id']: row for row in ideas}, exam_id) if table is not None else set()
    if (table is None or not complete or {row['idea_id'] for row in rows} - members
            or set(fallback_ids) & {row['idea_id'] for row in rows}):
        return {key: missing('Missing, incomplete or fallback mechanism evidence') for key in keys}
    membership = {key: group['mechanism_id'] for group in table['mechanisms'] for key in group['idea_ids']}
    counts = Counter(membership[row['idea_id']] for row in produced)
    baseline = {membership[row['idea_id']] for row in rows if row['level'] == 'baseline'}
    agents = defaultdict(set)
    for row in produced:
        agents[row['agent']].add(membership[row['idea_id']])
    overlap = [len(a & b) / len(a | b) for a, b in combinations(agents.values(), 2)]
    s = len(counts)
    f1, f2 = sum(n == 1 for n in counts.values()), sum(n == 2 for n in counts.values())
    remaining = f1 * (f1 - 1) / (2 * (f2 + 1))
    details = {'variant': 'bias_corrected_chao1', 'f1': f1, 'f2': f2}
    return {'unique_mechanisms': observed(s),
            'unique_ratio': observed(s / len(produced)) if produced else missing('Empty output denominator'),
            'outside_baseline_ratio': observed(len(counts.keys() - baseline) / s, baseline_scope='unit')
                if s and baseline else missing('Missing baseline or empty mechanism denominator', baseline_scope='unit'),
            'agent_overlap': observed(sum(overlap) / len(overlap), pairs=len(overlap))
                if overlap else missing('Fewer than two observed agent sets'),
            'chao1': observed(s + remaining, **details) if produced else missing('Empty pool', **details),
            'remaining_space': observed(remaining, **details) if produced else missing('Empty pool', **details)}


def tfidf(texts):
    counts = []
    for text in texts:
        if not isinstance(text, str):
            raise ValueError('Text corpus must contain strings')
        text = unicodedata.normalize('NFKC', text).casefold()
        counts.append(Counter(text[i:i + 2] for i in range(len(text) - 1)))
    df = Counter(term for row in counts for term in row)
    vectors = []
    for row in counts:
        weights = {term: n * (math.log((1 + len(texts)) / (1 + df[term])) + 1) for term, n in row.items()}
        norm = math.sqrt(sum(n * n for n in weights.values()))
        vectors.append({term: n / norm for term, n in weights.items()} if norm else {})
    return vectors


def cosine(a, b):
    return max(0.0, min(1.0, sum(n * b.get(term, 0) for term, n in a.items())))


def text_metrics(texts, *, ids=None, human_pool=None, vectors=None):
    if human_pool is not None and (not isinstance(human_pool, list) or
                                   any(not isinstance(text, str) for text in human_pool)):
        raise ValueError('Human pool must be a JSON string array')
    ids = list(map(str, range(len(texts)))) if ids is None else ids
    if len(ids) != len(texts) or len(set(ids)) != len(ids):
        raise ValueError('Text identities must be unique and complete')
    vectors = tfidf(texts) if vectors is None else vectors
    details = {'warning': '字符二元组 TF-IDF 词面粗估，不代表语义覆盖',
               'normalization': 'NFKC_casefold_char_bigram_count_smooth_idf_l2',
               'points': len(texts), 'pairs': len(texts) * (len(texts) - 1) // 2}
    result = {key: missing('Fewer than two texts or undefined zero vector', **details)
              for key in ('pair_similarity', 'mst')}
    # ceiling: exact all-pairs/Prim O(n^2), O(n) graph storage; profile before scaling exams.
    if len(vectors) >= 2 and all(vectors):
        pair_sum = sum(cosine(a, b) for a, b in combinations(vectors, 2))
        result['pair_similarity'] = observed(pair_sum / details['pairs'], **details)
        start = min(range(len(ids)), key=lambda i: ids[i])
        edges, total = [], 0.0
        best = {i: (1 - cosine(vectors[start], vectors[i]), ids[start], start)
                for i in range(len(ids)) if i != start}
        while best:
            target = min(best, key=lambda i: (best[i][0], best[i][1], ids[i]))
            distance, _, source = best.pop(target)
            edges.append([ids[source], ids[target]])
            total += distance
            for i in best:
                candidate = (1 - cosine(vectors[target], vectors[i]), ids[target], target)
                if candidate < best[i]:
                    best[i] = candidate
        result['mst'] = observed(total, mean_edge=total / (len(ids) - 1),
                                 edge_count=len(edges), edges=edges, **details)
    result['human_pool_distance'] = missing('Missing/empty human pool or undefined text vector', **details)
    if human_pool and texts:
        joined = tfidf([*texts, *human_pool])
        ai, human = joined[:len(texts)], joined[len(texts):]
        if all(joined):
            distances = [min(1 - cosine(a, b) for a in ai) for b in human]
            result['human_pool_distance'] = observed(sum(distances) / len(distances), distances=distances,
                                                     direction='lower_is_nearer', **details)
    return result


def type_metrics(rows, exam, *, premise=None, subproblems=None):
    rows = [row for row in rows if row['level'] != 'baseline']
    ids = {row['idea_id'] for row in rows}
    result = {key: missing('Missing independent semantic evidence') for key in
              ('color_ratio', 'premise_challenge_ratio', 'subproblem_coverage')}
    markers = [p['marker'] for p in exam.get('planted', []) if p['kind'] == 'color']
    if rows and markers:
        count = sum(any(marker in row['title'] or marker in row['text'] for marker in markers) for row in rows)
        result['color_ratio'] = observed(count / len(rows), numerator=count, denominator=len(rows), markers=markers)
    if premise is not None:
        if (not isinstance(premise, dict) or set(premise) != {'exam_id', 'wrong_premise', 'challenged'}
                or premise['exam_id'] != exam['id'] or premise['wrong_premise'] != exam.get('wrong_premise')
                or not isinstance(premise['challenged'], dict)
                or premise['challenged'].keys() - ids or any(type(v) is not bool for v in premise['challenged'].values())):
            raise ValueError('Invalid premise evidence identity, IDs or boolean labels')
        if ids and premise['challenged'].keys() == ids and exam.get('wrong_premise'):
            agents = {row['agent'] for row in rows}
            challenged = {row['agent'] for row in rows if premise['challenged'][row['idea_id']]}
            result['premise_challenge_ratio'] = observed(len(challenged) / len(agents),
                                                         numerator=len(challenged), denominator=len(agents))
    if subproblems is not None:
        if (not isinstance(subproblems, dict) or set(subproblems) != {'exam_id', 'targets', 'matches'}
                or subproblems['exam_id'] != exam['id'] or not isinstance(subproblems['targets'], list)
                or any(not isinstance(v, str) or not v for v in subproblems['targets'])
                or len(set(subproblems['targets'])) != len(subproblems['targets'])
                or not isinstance(subproblems['matches'], dict) or subproblems['matches'].keys() - ids):
            raise ValueError('Invalid subproblem evidence identity or target catalog')
        targets, hits = set(subproblems['targets']), set()
        for matched in subproblems['matches'].values():
            if not isinstance(matched, list) or any(not isinstance(v, str) or v not in targets for v in matched):
                raise ValueError('Invalid subproblem target mapping')
            hits.update(matched)
        if targets and ids and subproblems['matches'].keys() == ids:
            result['subproblem_coverage'] = observed(len(hits) / len(targets), numerator=len(hits),
                                                     denominator=len(targets), matched=sorted(hits))
    return result


def call_costs(calls, *, evidence=None):
    keys = [row['call_key'] for row in calls]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate Call receipt; costs cannot be counted twice')
    if evidence is not None and evidence['status'] != 'ok':
        fields = ('calls', 'usd', 'estimated_usd', 'paid_error_calls', 'paid_error_usd', 'tokens', 'duration_ms')
        return {**{key: missing(evidence['reason']) for key in fields},
                'observed_partial': {'scope': 'observed_receipts_only_not_complete_unit', 'costs': call_costs(calls)}}
    fields = ('input', 'cache_write', 'cache_read', 'output')
    for row in calls:
        if row.get('usd') is not None:
            amount(row['usd'])
        if row.get('tokens') is not None:
            token_counts(row['tokens'])
    tokens_complete = all(isinstance(row.get('tokens'), dict) and set(row['tokens']) == set(fields)
                          and row['outcome'] != 'uncertain' for row in calls)
    totals = {key: sum(row['tokens'][key] for row in calls) for key in fields} if tokens_complete else None
    if totals is not None:
        totals['total'] = totals['input'] + totals['cache_write'] + totals['output']
    duration_complete = all(type(row.get('duration_ms')) is int and row['duration_ms'] >= 0 for row in calls)
    usd_complete = all(row.get('usd') is not None for row in calls)
    estimated_complete = usd_complete and all(type(row.get('usd_estimated')) is bool for row in calls)
    errors = [row for row in calls if row['outcome'] == 'paid_error']
    return {'calls': observed(len(calls)),
            'usd': observed(exact_sum(0, *(amount(row['usd']) for row in calls))) if usd_complete else missing('Missing Call USD'),
            'estimated_usd': observed(exact_sum(0, *(amount(row['usd']) for row in calls if row['usd_estimated'])))
                if estimated_complete else missing('Missing Call USD or estimation flag'),
            'paid_error_calls': observed(len(errors)),
            'paid_error_usd': observed(exact_sum(0, *(amount(row['usd']) for row in errors)))
                if all(row.get('usd') is not None for row in errors) else missing('Missing paid-error USD'),
            'tokens': observed(totals) if tokens_complete else missing('Missing tokens or uncertain Call'),
            'duration_ms': observed(sum(row['duration_ms'] for row in calls), scope='accumulated_call_duration')
                if duration_complete else missing('Missing Call duration', scope='accumulated_call_duration')}


def guard_evidence(summary, constraint=None):
    default = {'status': 'not_evaluated', 'violations': None}
    injection = deepcopy((summary or {}).get('guards', {}).get('injection', default))
    constraint = deepcopy(constraint if constraint is not None else
                          (summary or {}).get('guards', {}).get('constraint', default))
    for guard in (injection, constraint):
        if (not isinstance(guard, dict) or guard.get('status') not in ('checked', 'not_applicable', 'not_evaluated')
                or (guard.get('violations') is not None and
                    (type(guard['violations']) is not int or guard['violations'] < 0))
                or guard['status'] == 'checked' and guard.get('violations') is None):
            raise ValueError('Invalid guard evidence')
    return {'injection': injection, 'constraint': constraint}


def exhaustion(batches, *, expected=None, complete=True):
    result, seen = {}, set()
    latest = max((LEVELS.index(row['level']) for row in expected or []), default=-1)
    for level in LEVELS:
        if level == 'baseline':
            result[level] = missing('Baseline growth is not recorded; baseline is a reference, not zero growth')
            continue
        selected = [row for row in batches or [] if row['level'] == level]
        if expected is not None:
            committed = [row for row in expected if row['level'] == level]
            if selected != committed or not complete and LEVELS.index(level) >= latest:
                result[level] = missing('Level batches incomplete, pending or public projection stale')
                seen.update(key for row in committed for key in row.get('new_mechanism_ids') or [])
                continue
        elif not complete:
            result[level] = missing('Missing authoritative level-completion evidence')
            continue
        if not selected or any(row['status'] != 'ok' or row.get('fallback_ids')
                               or row.get('new_mechanism_ids') is None for row in selected):
            result[level] = missing('Missing/incomplete loop batch evidence')
            continue
        ids = {key for row in selected for key in row['new_mechanism_ids']}
        result[level] = observed(len(ids - seen), mechanism_ids=sorted(ids - seen))
        seen.update(ids)
    return result


def unit_exhaustion(data):
    return exhaustion(data.get('batches'), expected=data.get('loop_projection'),
                      complete=data.get('loop_complete') is True)


def noise_line(scores):
    if not isinstance(scores, dict):
        raise ValueError('Noise requires config -> numeric score sequences')
    for seq in scores.values():
        if not isinstance(seq, (list, tuple)) or any(isinstance(v, bool) or not isinstance(v, (int, float, Decimal))
                                                     or not Decimal(str(v)).is_finite() for v in seq):
            raise ValueError('Noise scores must be finite numbers, not bool')
    if not scores or any(len(seq) < 3 for seq in scores.values()):
        return missing('Every config requires at least three repetitions')
    ranges = {key: max(Decimal(str(v)) for v in seq) - min(Decimal(str(v)) for v in seq) for key, seq in scores.items()}
    return observed(max(ranges.values()), ranges=ranges)


def project_table(table, rows):
    if table is None:
        return None
    by_id = {row['idea_id']: row for row in rows}
    groups = []
    for group in table['mechanisms']:
        members = [key for key in group['idea_ids'] if key in by_id]
        if members:
            groups.append({**group, 'idea_ids': members, 'representative': members[0],
                           'in_baseline': any(by_id[key]['level'] == 'baseline' for key in members)})
    return {'exam_id': table['exam_id'], 'mechanisms': groups}


def linked_calls(rows, calls):
    """Validate linkage without turning lost/unknown measurements into zero."""
    by_key = {}
    for call in calls:
        if call['call_key'] in by_key:
            raise ValueError('Duplicate Call identity')
        by_key[call['call_key']] = call
    for row in rows:
        call = by_key.get(row['call_key'])
        if call is None:
            return 'Idea has no matching Call receipt'
        if (any(call[key] != row[key] for key in ('exam_id', 'config', 'rep', 'agent', 'model', 'level'))
                or call['purpose'] != ('rewrite' if row['returned'] else 'generate') or call['outcome'] != 'ok'):
            return 'Call/Idea lineage or outcome mismatch'
    return None


def comparison_calls(data, seed):
    calls = [row for row in data['calls'] if row['purpose'] in ('generate', 'rewrite') and row['level'] != 'baseline']
    calls.sort(key=lambda row: row['call_key'])
    random.Random(seed).shuffle(calls)
    return calls


def compare_units(a, b, *, seed, idea_count=None, token_cap=None, replaced=False):
    if type(seed) is not int:
        raise ValueError('Comparison seed must be an integer')
    if idea_count is not None:
        positive_int(idea_count, 'idea_count')
    if token_cap is not None and (type(token_cap) is not int or token_cap < 0):
        raise ValueError('token_cap must be a nonnegative integer')
    sides = {'a': a, 'b': b}
    output = {}
    identity = []
    for data in sides.values():
        rows = data['ideas']
        if rows:
            checked_ideas(rows, rows[0]['exam_id'])
            if data.get('table') is not None:
                partition_members(data['table'], {row['idea_id']: row for row in rows}, rows[0]['exam_id'])
            units = {(row['exam_id'], row['config'], row['rep']) for row in rows}
            if len(units) != 1:
                raise ValueError('Comparison sides must each contain one complete unit')
            identity.append(next(iter(units)))
    # A replacement takes the place of the unit it replaced, so that one pair may hold different repetition numbers.
    if len(identity) == 2 and (identity[0][0] != identity[1][0] or (identity[0][2] != identity[1][2] and not replaced)
                                or identity[0][1] == identity[1][1]):
        raise ValueError('Compare different configs of the same exam and rep')
    ordered = {key: comparison_calls(data, seed) for key, data in sides.items()}
    grouped = {}
    for key, data in sides.items():
        grouped[key] = defaultdict(list)
        for row in data['ideas']:
            if row['level'] != 'baseline':
                grouped[key][row['call_key']].append(row)
    pools = {key: [row for call in ordered[key] for row in grouped[key][call['call_key']]] for key in sides}
    available = {key: len(rows) for key, rows in pools.items()}
    n = min(available.values()) if idea_count is None else idea_count
    for mode in ('same_ideas', 'same_tokens'):
        result = observed(None, seed=seed, available=available, mechanism_scope='projected_original_partition')
        required = [result for data in sides.values() for result in (recording_completion(data), receipt_evidence(data))]
        if any(result['status'] != 'ok' for result in required):
            output[mode] = missing(next(result['reason'] for result in required if result['status'] != 'ok'))
            continue
        if mode == 'same_ideas':
            result['requested'] = n
            if not n or any(count < n for count in available.values()):
                output[mode] = missing('Insufficient ideas for requested common N', requested=n, available=available)
                continue
            selected = {key: rows[:n] for key, rows in pools.items()}
            chosen = {}
            for key in sides:
                # Failed/zero-output calls before the cutoff still cost money; never erase them.
                last_key = selected[key][-1]['call_key']
                cutoff = next(i for i, call in enumerate(ordered[key]) if call['call_key'] == last_key)
                chosen[key] = ordered[key][:cutoff + 1]
        else:
            costs = {key: call_costs(calls)['tokens'] for key, calls in ordered.items()}
            if any(item['status'] != 'ok' for item in costs.values()):
                output[mode] = missing('Missing tokens or uncertain Call; no imputation')
                continue
            cap = min(item['value']['total'] for item in costs.values()) if token_cap is None else token_cap
            result.update(cap=cap, warning='相同 token 上限，非精确相同实耗；历史调用种子重排，不代表生成时序前缀；不跳过大调用')
            selected, chosen = {}, {}
            for key, calls in ordered.items():
                used, chosen[key] = 0, []
                for call in calls:
                    total = sum(call['tokens'][field] for field in ('input', 'cache_write', 'output'))
                    if used + total > cap:
                        break
                    chosen[key].append(call)
                    used += total
                keys = {call['call_key'] for call in chosen[key]}
                selected[key] = [row for row in pools[key] if row['call_key'] in keys]
            if any(not rows for rows in selected.values()):
                audit = {}
                for key, calls in chosen.items():
                    cost = call_costs(calls)
                    used = cost['tokens']['value']['total']
                    audit[key] = {'tokens': used, 'unused': cap - used, 'costs': cost,
                                  'call_keys': [call['call_key'] for call in calls],
                                  'idea_ids': [row['idea_id'] for row in selected[key]]}
                output[mode] = missing('Common token cap admits no output batch on a side', cap=cap,
                                       warning=result['warning'], **audit)
                continue
        texts = {key: [row['title'] + '\n' + row['text'] for row in rows] for key, rows in selected.items()}
        joined = tfidf(texts['a'] + texts['b'])
        for key, data in sides.items():
            rows, calls = selected[key], chosen[key]
            baseline = [row for row in data['ideas'] if row['level'] == 'baseline']
            projected = baseline + rows
            unit = rows[0]
            proxy = mechanism_metrics(projected, project_table(data.get('table'), projected),
                exam_id=unit['exam_id'], config=unit['config'], rep=unit['rep'], fallback_ids=data.get('fallback_ids', []))
            vectors = joined[:len(texts['a'])] if key == 'a' else joined[len(texts['a']):]
            proxy.update(text_metrics(texts[key], ids=[r['idea_id'] for r in rows], vectors=vectors))
            population = unit_completion(data)
            if population['status'] != 'ok':
                proxy = {name: missing(population['reason'], observed_subset=value) for name, value in proxy.items()}
            cost = call_costs(calls)
            selected_counts = Counter(row['call_key'] for row in rows)
            result[key] = {'idea_ids': [row['idea_id'] for row in rows], 'call_keys': [call['call_key'] for call in calls],
                           'partial_call': any(selected_counts[call['call_key']] != len(grouped[key][call['call_key']])
                                               for call in calls),
                           'costs': cost, 'cost_policy': 'whole_call_prefix_no_proration', 'metrics': proxy,
                           'text_space': 'joint_selected_corpus',
                           'population': population,
                           'baseline_costs': call_costs([c for c in data['calls'] if c['level'] == 'baseline']),
                           'shared_costs': data.get('shared_costs', missing('Shared stage cost not supplied'))}
            if mode == 'same_tokens':
                used = cost['tokens']['value']['total']
                result[key].update(tokens=used, unused=cap - used)
        result['value'] = {'a': len(selected['a']), 'b': len(selected['b'])}
        if mode == 'same_tokens':
            result['token_difference'] = result['a']['tokens'] - result['b']['tokens']
        if any(result[key]['population']['status'] != 'ok' for key in sides):
            result['observed_selection'] = result['value']
            result.update(value=None, status='not_evaluated', reason='Source population incomplete; selection/costs are observed accounting only')
        output[mode] = result
    for result in output.values():
        result['units'] = {key: data.get('unit') or
            ({field: data['ideas'][0][field] for field in ('exam_id', 'config', 'rep')} if data['ideas'] else None)
            for key, data in sides.items()}
    return output


def load_unit(run_dir, exam_id, config, rep):
    """Read public projections only, never recover or rewrite a source ledger/state."""
    root = Path(run_dir).resolve()
    safe_segment(exam_id)
    safe_segment(config)
    positive_int(rep, 'rep')
    unit = root / exam_id / config / f'rep-{rep}'
    paths = {key: unit / name for key, name in [('ideas', 'ideas.jsonl'), ('calls', 'calls.jsonl'),
             ('summary', 'run_summary.json'), ('batches', 'loop_batches.jsonl'), ('loop', 'loop_state.json')]}
    paths.update(table=root / exam_id / 'mechanisms.json',
                 shared_calls=root / exam_id / 'dedupe/calls.jsonl',
                 dedupe_state=root / exam_id / 'dedupe/state.json')
    all_rows = []
    for path in sorted((root / exam_id).glob('*/rep-*/ideas.jsonl')):
        source = read_rows(path)
        if any((r['exam_id'], r['config'], f"rep-{r['rep']}") != (exam_id, path.parent.parent.name, path.parent.name)
               for r in source):
            raise ValueError('Recording file disagrees with unit lineage')
        all_rows.extend(source)
        paths[str(path.relative_to(root))] = path
    checked_ideas(all_rows, exam_id)
    rows = read_rows(paths['ideas'])
    table = read_file(paths['table']) if paths['table'].is_file() else None
    if table is not None:
        partition_members(table, {row['idea_id']: row for row in all_rows}, exam_id)
    calls = read_rows(paths['calls'])
    if any((call['exam_id'], call['config'], call['rep']) != (exam_id, config, rep) for call in calls):
        raise ValueError('Call receipt belongs to another unit')
    summary = read_file(paths['summary']) if paths['summary'].is_file() else {}
    batches = read_rows(paths['batches']) if paths['batches'].is_file() else None
    loop_state = None
    if paths['loop'].is_file():
        from .record import digest
        envelope = read_file(paths['loop'])
        loop_state = envelope.get('state')
        if not isinstance(loop_state, dict) or envelope.get('digest') != digest(loop_state):
            raise ValueError('Invalid source loop state digest')
    fallback = []
    classification = None
    if paths['dedupe_state'].is_file():
        classification = read_file(paths['dedupe_state'])['state']
        fallback = classification['fallback_ids']
    shared_calls = read_rows(paths['shared_calls'])
    shared_evidence = observed(True) if (paths['shared_calls'].is_file() and classification is not None
        and not classification.get('pending') and classification.get('calls') == shared_calls) else missing(
            'Shared dedupe receipts missing, incomplete or classification still pending')
    return {'unit': {'exam_id': exam_id, 'config': config, 'rep': rep},
            'ideas': rows, 'calls': calls, 'table': project_table(table, rows),
            'summary': summary, 'batches': batches, 'loop_present': loop_state is not None,
            'loop_complete': loop_state.get('complete') if loop_state else None,
            'loop_projection_current': loop_state.get('projection') == batches if loop_state else None,
            'loop_projection': loop_state.get('projection') if loop_state else None,
            'fallback_ids': fallback, 'calls_present': paths['calls'].is_file(), 'ideas_present': paths['ideas'].is_file(),
            'shared_costs': call_costs(shared_calls, evidence=shared_evidence),
            'source_hashes': {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                              if path.is_file() else None for path in paths.values()}}


def compute_unit(data, *, exam_id, config, rep, exam=None, human_pool=None, premise=None,
                 subproblems=None, constraint=None, scope='authoritative', replay_calls=None):
    rows = unit_rows(data['ideas'], exam_id, config, rep)
    produced = [row for row in rows if row['level'] != 'baseline']
    completion = unit_completion(data)
    complete = completion['status'] == 'ok'
    linkage = receipt_evidence(data)
    metrics = mechanism_metrics(rows, data.get('table'), exam_id=exam_id, config=config, rep=rep,
                                 complete=complete, fallback_ids=data.get('fallback_ids', []))
    metrics.update(text_metrics([r['title'] + '\n' + r['text'] for r in produced],
                                ids=[r['idea_id'] for r in produced], human_pool=human_pool))
    metrics.update(type_metrics(rows, exam or {'id': exam_id}, premise=premise, subproblems=subproblems))
    if not complete:
        metrics = {key: missing(completion['reason'],
                                  observed_subset=result) for key, result in metrics.items()}
    recorded = recording_completion(data)
    source_costs = call_costs(data['calls'], evidence=linkage if linkage['status'] != 'ok' else recorded)
    guards = guard_evidence(data.get('summary'), constraint)
    if not complete:
        for key, guard in guards.items():
            if guard['status'] == 'checked':
                guards[key] = {**guard, 'status': 'not_evaluated', 'violations': guard['violations'] or None,
                               'reason': completion['reason'], 'scope': 'observed_recording_prefix'}
    return {'schema_version': 1, 'scope': scope, 'unit': {'exam_id': exam_id, 'config': config, 'rep': rep},
            'counts': {'baseline': len(rows) - len(produced), 'produced': len(produced), 'total': len(rows)},
            'metrics': metrics, 'guards': guards,
            'exhaustion': unit_exhaustion(data),
            'costs': {'source_recording': source_costs, 'shared_dedupe': data.get('shared_costs', missing('Not supplied')),
                      'replay': call_costs(replay_calls) if replay_calls is not None else missing('Not a replay')},
            'linkage': linkage, 'completion': completion, 'recording_completion': recorded}


def save_metrics(run_dir, result):
    root = Path(run_dir).resolve()
    path = root / 'metrics.json'
    if path.resolve() != path or (root / 'metrics_state').resolve() != root / 'metrics_state':
        raise ValueError('Metrics output cannot be a symlink alias')
    with recording_lock(root / 'metrics_state'):
        output = read_file(path) if path.exists() else {'schema_version': 1, 'exams': {}}
        if output.get('schema_version') != 1 or not isinstance(output.get('exams'), dict):
            raise ValueError('Unsupported metrics output schema')
        unit = result['unit']
        output['exams'].setdefault(unit['exam_id'], {}).setdefault(unit['config'], {})[str(unit['rep'])] = result
        atomic_write(path, dumps(output) + '\n')
    return output


def summary_text(result):
    unit = result['unit']
    lines = [f"指标摘要 {unit['exam_id']}/{unit['config']}/rep-{unit['rep']}", '指标 | 数值 | 状态/原因']
    for key, entry in result['metrics'].items():
        lines.append(f"{key} | {entry['value']} | {entry.get('reason', entry['status'])}")
    lines.append('字符二元组 TF-IDF 是词面粗估，不代表语义覆盖；缺测不计零。')
    lines.append(f"完整性: {dumps(result.get('completion', missing('Not supplied')))}")
    lines.append(f"linkage: {dumps(result.get('linkage', missing('Not supplied')))}")
    lines.append(f"护栏: {dumps(result['guards'])}")
    lines.append(f"枯竭: {dumps(result['exhaustion'])}")
    for key, cost in result['costs'].items():
        lines.append(f"成本范围 {key}: {dumps(cost)}")
    comparisons = result.get('comparisons')
    for key, label in [('same_ideas', '相同点子数'), ('same_tokens', '相同 token 上限，非精确相同实耗')]:
        if comparisons and comparisons[key]['status'] == 'ok':
            comparison = comparisons[key]
            lines.append(f"{label}: {comparison['status']} cap={comparison.get('cap')} N={comparison.get('requested')}")
            for side in ('a', 'b'):
                entry = comparison[side]
                unit = comparison['units'][side]
                lines.append(f"{side} ({unit['config']}/rep-{unit['rep']}): ideas={len(entry['idea_ids'])} tokens={entry.get('tokens')} "
                              f"unused={entry.get('unused')} usd={entry['costs']['usd']['value']}")
            if key == 'same_tokens':
                lines.append(f"实耗差 (a-b): {comparison['token_difference']} token；历史重排不代表生成时序前缀。")
        else:
            lines.append(f"{label}: {dumps(comparisons[key]) if comparisons else 'not_evaluated: 未提供比较配置'}")
    return '\n'.join(lines)


def run_cli(args):
    label = f'metrics {args.exam_id}/{args.config_name}/rep-{args.rep}'
    try:
        data = load_unit(args.run_dir, args.exam_id, args.config_name, args.rep)
        exam = read_file(args.exam) if args.exam else None
        if exam is not None and exam.get('id') != args.exam_id:
            raise ValueError('Exam evidence identity mismatch')
        result = compute_unit(data, exam_id=args.exam_id, config=args.config_name, rep=args.rep, exam=exam,
            human_pool=read_file(args.human_pool) if args.human_pool and args.human_pool.exists() else None,
            premise=read_file(args.premise_labels) if args.premise_labels else None,
            subproblems=read_file(args.subproblems) if args.subproblems else None)
        if args.compare:
            result['comparisons'] = compare_units(data, load_unit(args.run_dir, args.exam_id, args.compare, args.rep),
                seed=args.seed, idea_count=args.idea_count, token_cap=args.token_cap)
        save_metrics(args.run_dir, result)
        print(summary_text(result))
    except (ValueError, OSError) as exc:
        raise ValueError(f'{label}: {exc}') from exc


def register(subparsers):
    parser = subparsers.add_parser('metrics', help='Evidence-aware machine metrics and two comparison controls')
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--exam-id', required=True)
    parser.add_argument('--config-name', required=True)
    parser.add_argument('--rep', type=int, default=1)
    parser.add_argument('--exam', type=Path)
    parser.add_argument('--human-pool', type=Path)
    parser.add_argument('--premise-labels', type=Path)
    parser.add_argument('--subproblems', type=Path)
    parser.add_argument('--compare')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--idea-count', type=int)
    parser.add_argument('--token-cap', type=int)
    parser.set_defaults(func=lambda args: cli_boundary(run_cli, args))
