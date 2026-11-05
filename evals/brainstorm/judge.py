"""Reference-only anonymous rankings and budgeted hard-constraint evaluation."""

from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re

from .backends import FakeBackend, NotStarted
from .budget import LedgerError, atomic_write, guarded_call
from .config import positive_int
from .dedupe import checked_ideas
from .exam import safe_segment
from .metrics import dumps, missing
from .prompts import wrap_data
from .record import MeasuredBackend, digest, make_backend, recording_lock, sync_call


def stage_backend(config, response):
    """Never install the generation fixture on a downstream fake backend."""
    options = config['backend']
    if options['kind'] == 'fake':
        return FakeBackend(**{key: value for key, value in options.items() if key != 'kind'} |
                           ({} if 'responses' in options else {'responses': response}))
    return make_backend(config)


def check_exam(exam):
    """Validate every field consumed downstream before the first paid boundary."""
    if not isinstance(exam, dict) or not isinstance(exam.get('id'), str):
        raise ValueError('Exam requires a string id')
    safe_segment(exam['id'])
    if not isinstance(exam.get('question'), str) or not exam['question'].strip():
        raise ValueError('Exam requires a nonempty question')
    if (not isinstance(exam.get('constraints'), list)
            or any(not isinstance(value, str) or not value.strip() for value in exam['constraints'])):
        raise ValueError('Exam requires a constraints string array')
    if (not isinstance(exam.get('planted', []), list) or any(not isinstance(row, dict)
            or not isinstance(row.get('kind'), str) or not isinstance(row.get('marker'), str)
            or not row['marker'] for row in exam.get('planted', []))):
        raise ValueError('Invalid planted marker evidence')


def safe_output(path, filenames):
    path = Path(path).absolute()
    if path.resolve() != path or any((path / name).resolve() != path / name for name in filenames):
        raise ValueError('Downstream output cannot contain symlink aliases')
    return path


def read_state(path):
    """Verify the exact persisted state bytes, including old float spellings."""
    text = path.read_text(encoding='utf-8')
    decoder = json.JSONDecoder(parse_float=Decimal)
    envelope = decoder.decode(text)
    if (not isinstance(envelope, dict) or set(envelope) != {'digest', 'state'}
            or not isinstance(envelope['state'], dict) or not isinstance(envelope['digest'], str)
            or not re.fullmatch('[a-f0-9]{64}', envelope['digest'])):
        raise ValueError('Invalid judge state envelope')
    # The fixed envelope has only digest and state; its first state key is the outer one.
    marker = re.search(r'"state"\s*:\s*', text)
    if marker is None:
        raise ValueError('Invalid judge state encoding')
    state, end = decoder.raw_decode(text, marker.end())
    raw_digest = hashlib.sha256(text[marker.end():end].encode()).hexdigest()
    if state != envelope['state'] or raw_digest != envelope['digest']:
        raise ValueError('Invalid judge state digest')
    return state


def parse_response(text, items, mode):
    def unique_object(pairs):
        value = {}
        for key, entry in pairs:
            if key in value:
                raise ValueError('Duplicate response field')
            value[key] = entry
        return value
    try:
        data = json.loads(text, object_pairs_hook=unique_object)
        key = 'verdicts' if mode == 'constraint' else 'ranking'
        if not isinstance(data, dict) or set(data) != {key} or not isinstance(data[key], list):
            raise ValueError('Invalid response envelope')
        expected, seen = {row['item_id'] for row in items}, []
        for row in data[key]:
            fields = {'item_id', 'violation', 'reason'} if mode == 'constraint' else {'item_id', 'score'}
            if not isinstance(row, dict) or set(row) != fields or not isinstance(row['item_id'], str):
                raise ValueError('Invalid response row')
            seen.append(row['item_id'])
            if mode == 'constraint':
                if (type(row['violation']) is not bool or not isinstance(row['reason'], str)
                        or row['violation'] and not row['reason'].strip()):
                    raise ValueError('Invalid constraint verdict')
            elif (type(row['score']) not in (int, float)
                  or isinstance(row['score'], float) and not math.isfinite(row['score'])):
                raise ValueError('Reference scores must be finite numbers')
        if len(seen) != len(expected) or set(seen) != expected:
            raise ValueError('Response must cover every anonymous item exactly once')
        return data[key]
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        # Do not echo model text: it can contain private questions or embedded instructions.
        raise ValueError('Invalid judge response; complete unique items and valid values required') from exc


def fake_judge(prompt, model, max_output_tokens):
    """Deterministic wiring fixture, not evidence of semantic ranking quality."""
    block = prompt.rsplit('\nItems:\n', 1)[1].split('\n')
    items = json.loads('\n'.join(block[2:-1]))
    if prompt.startswith('Check hard constraints'):
        return dumps({'verdicts': [{'item_id': row['item_id'], 'violation': False, 'reason': ''} for row in items]})
    return dumps({'ranking': [{'item_id': row['item_id'], 'score': len(items) - i} for i, row in enumerate(items)]})


def save_state(path, state):
    encoded = dumps(state)
    atomic_write(path / 'state.json', '{"digest":' + json.dumps(hashlib.sha256(encoded.encode()).hexdigest())
                 + ',"state":' + encoded + '}\n')


def export_state(path, state):
    save_state(path, state)
    for name in ('calls', 'failures'):
        atomic_write(path / f'{name}.jsonl', ''.join(dumps(row) + '\n' for row in state[name]))
    if state.get('result') is not None:
        atomic_write(path / 'result.json', dumps(state['result']) + '\n')


def sync_failed(output_dir, ledger):
    """Bring a failed check's receipt up to the ledger without calling anything, once its unit is replaced."""
    path = safe_output(output_dir, ('state.json', 'calls.jsonl', 'failures.jsonl', 'result.json', 'record.lock'))
    with recording_lock(path):
        state = read_state(path / 'state.json')
        sync_call(state, next(iter(state['requests'])), ledger)
        export_state(path, state)


def evaluate(ideas, exam, output_dir, *, backend, ledger, model, mode='rank', max_output_tokens=2048):
    check_exam(exam)
    if mode not in ('rank', 'self', 'constraint'):
        raise ValueError('Unknown judge mode')
    positive_int(max_output_tokens, 'max_output_tokens')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('Judge requires an explicit model')
    rows = list(checked_ideas(ideas, exam['id']).values())
    units = {(row['config'], row['rep']) for row in rows}
    if len(units) > 1:
        raise ValueError('Judge scope must be one config/rep unit')
    if mode == 'constraint' and not exam['constraints']:
        return {'status': 'not_applicable', 'violations': None, 'mode': mode, 'evaluator_model': model}
    if not rows:
        return {**missing('Empty evaluation universe'), 'mode': mode, 'evaluator_model': model,
                **({'violations': None} if mode == 'constraint' else {})}
    items = [{'item_id': f'x-{i}', 'title': row['title'], 'text': row['text']} for i, row in enumerate(rows)]
    lookup = {item['item_id']: row['idea_id'] for item, row in zip(items, rows)}
    instruction = ('Check hard constraints for every item. Return only JSON '
                   '{"verdicts":[{"item_id":"x-0","violation":false,"reason":""}]}.' if mode == 'constraint' else
                   'Rank every item without deleting any. Return only JSON '
                   '{"ranking":[{"item_id":"x-0","score":1.0}]} in best-first order. Scores are reference only.')
    prompt = '\n'.join([instruction, 'Question:', wrap_data(exam['question']),
                        'Constraints:', wrap_data(exam['constraints']), 'Items:', wrap_data(items)])
    path = safe_output(output_dir, ('state.json', 'calls.jsonl', 'failures.jsonl', 'result.json', 'record.lock'))
    identity = {'ideas': rows, 'exam': exam, 'model': model, 'mode': mode, 'backend': backend.name,
                'ledger': str(ledger.path), 'max_output_tokens': max_output_tokens}
    with recording_lock(path):
        save = lambda: save_state(path, state)
        export = lambda: export_state(path, state)
        if (path / 'state.json').exists():
            state = read_state(path / 'state.json')
            if dumps(state['identity']) != dumps(identity):
                raise ValueError('Judge input identity conflict')
        else:
            if any((path / name).exists() for name in ('calls.jsonl', 'failures.jsonl', 'result.json')):
                raise ValueError('Missing judge state with surviving outputs')
            key = 'judge_' + digest([str(path), dumps(identity)])
            request = {'call_key': key, 'exam_id': exam['id'], 'config': rows[0]['config'], 'rep': rows[0]['rep'],
                       'agent': 1, 'level': 'round1', 'purpose': 'constraint' if mode == 'constraint' else 'judge',
                       'backend': backend.name, 'model': model, 'attempt': 1, 'started_at': None, 'duration_ms': None}
            state = {'identity': identity, 'requests': {key: request}, 'calls': [], 'failures': [], 'result': None}
            save()
        if state['result'] is not None:
            export()
            return deepcopy(state['result'])
        if state.get('invalid_response'):
            export()
            raise ValueError('Invalid judge response already charged; no automatic retry')
        key, request = next(iter(state['requests'].items()))
        measured = MeasuredBackend(backend, request, save)
        try:
            response = guarded_call(ledger, measured, key, prompt, model, max_output_tokens)
        except (LedgerError, NotStarted) as exc:
            sync_call(state, key, ledger)
            failure = {'stage': mode, 'call_key': key, 'reason': type(exc).__name__, 'detail': str(exc)}
            if failure not in state['failures']:
                state['failures'].append(failure)
            export()
            raise
        if measured.persistence_error is not None:
            raise measured.persistence_error
        sync_call(state, key, ledger)
        try:
            verdicts = parse_response(response.text, items, mode)
        except ValueError:
            state['invalid_response'] = True
            state['failures'].append({'stage': mode, 'call_key': key, 'reason': 'invalid_response',
                                      'detail': 'Complete unique items and valid values required'})
            export()
            raise
        decoded = [{'idea_id': lookup[row['item_id']], **{k: v for k, v in row.items() if k != 'item_id'}} for row in verdicts]
        state['result'] = {'status': 'checked' if mode == 'constraint' else 'ok', 'mode': mode,
                           'evaluator_model': model, 'scope': {'exam_id': exam['id'], 'units': [list(u) for u in sorted(units)]},
                           'input_ids': [row['idea_id'] for row in rows],
                           'verdicts' if mode == 'constraint' else 'ranking': decoded}
        if mode == 'constraint':
            state['result']['violations'] = sum(row['violation'] for row in decoded)
        state['failures'] = []
        export()
        return deepcopy(state['result'])


def self_evaluate(ideas, exam, output_dir, *, backend, ledger, allowed_models, max_output_tokens=2048):
    result = {}
    for model in sorted({row['model'] for row in ideas}):
        subset = [row for row in ideas if row['model'] == model]
        if model not in allowed_models:
            result[model] = {**missing('Generator model not in allowed models'), 'evaluator_model': model,
                             'mode': 'self', 'input_ids': [row['idea_id'] for row in subset]}
        else:
            result[model] = evaluate(subset, exam, Path(output_dir) / digest(model), backend=backend,
                ledger=ledger, model=model, mode='self', max_output_tokens=max_output_tokens)
    return result
