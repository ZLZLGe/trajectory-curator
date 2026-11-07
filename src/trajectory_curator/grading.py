"""Resumable user-only model annotation using the pinned upstream prompt and rubric."""
import json
import os
import random
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from .common import atomic_json, digest, dumps, file_hash, now, read_jsonl
from .vendor.inputs import build_payload, normalize_item
from .vendor.rubric import load_rubric
from .vendor.scoring import derive
from .vendor.storage import strict_json
from .vendor.validation import validate_output

RESOURCES = Path(__file__).parent / 'resources'


def parse_output(content):
    if not isinstance(content, str) or not content.strip():
        raise ValueError('empty_model_output')
    text = content.strip()
    match = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n?```', text, re.S | re.I)
    if match:
        text = match.group(1)
    obj = strict_json(text)
    if not isinstance(obj, dict):
        raise ValueError('model_output_not_object')
    return obj


class FatalEndpointError(RuntimeError):
    pass


def annotate(item, *, api_key, config, rubric, system, post=requests.post, stop=None):
    payload, stats = build_payload(normalize_item(item), turn_cap=120000, total_budget=120000)
    if stats['truncated_turns'] or stats['budget_exceeded']:
        raise ValueError('full_user_input_required')
    input_hash = digest(dumps(payload).encode())
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': dumps(payload)}]
    start = time.monotonic()
    attempts, usage = [], Counter()
    response_data = None
    max_tokens = config['max_tokens']
    last_error = 'no_response'
    for semantic in range(3):
        body = {'model': config['model'], 'temperature': 0, 'max_tokens': max_tokens,
                'reasoning_effort': config['reasoning_effort'], 'response_format': {'type': 'json_object'},
                'messages': messages}
        response_data = None
        for retry in range(5):
            if stop is not None and stop.is_set():
                raise FatalEndpointError('batch_stopped')
            attempt = {'semantic_attempt': semantic + 1, 'network_attempt': retry + 1, 'max_tokens': max_tokens}
            t0 = time.monotonic()
            try:
                response = post(config['endpoint'], json=body,
                                headers={'Authorization': 'Bearer ' + api_key}, timeout=(30, 600))
                attempt['http_status'] = response.status_code
                if response.status_code in (401, 403, 404):
                    if stop is not None:
                        stop.set()
                    raise FatalEndpointError('HTTP_' + str(response.status_code))
                if response.status_code == 200:
                    response_data = response.json()
                    if (not isinstance(response_data, dict) or not isinstance(response_data.get('choices'), list)
                            or not response_data['choices'] or not isinstance(response_data['choices'][0], dict)
                            or not isinstance(response_data['choices'][0].get('message'), dict)):
                        raise ValueError('missing_choices')
                    attempt['outcome'] = 'response'
                else:
                    last_error = 'HTTP_' + str(response.status_code)
                    attempt['outcome'] = last_error
                    if response.status_code not in (408, 409, 425, 429) and response.status_code < 500:
                        retry = 4
            except FatalEndpointError:
                raise
            except (requests.RequestException, ValueError) as exc:
                response_data = None
                last_error = type(exc).__name__
                attempt['outcome'] = last_error
            attempt['elapsed_seconds'] = round(time.monotonic() - t0, 3)
            attempts.append(attempt)
            if response_data is not None:
                break
            if retry == 4:
                break
            time.sleep(min(30, 2 ** retry) * random.uniform(.8, 1.2))
        if response_data is None:
            break
        for k in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            value = (response_data.get('usage') or {}).get(k)
            if type(value) is int:
                usage[k] += value
        choice = response_data['choices'][0]
        content = (choice.get('message') or {}).get('content')
        finish = choice.get('finish_reason')
        attempts[-1]['finish_reason'] = finish
        try:
            if finish not in ('stop', 'end_turn', None):
                if finish == 'length':
                    max_tokens = min(65536, max_tokens * 2)
                raise ValueError('unaccepted_finish_reason:' + str(finish))
            obj = parse_output(content)
            validate_output(obj, [t['turn_id'] for t in item['user_turns']], rubric)
        except (ValueError, TypeError, KeyError) as exc:
            last_error = str(exc)[:300]
            attempts[-1]['validation_error'] = last_error
            if semantic < 2:
                messages = messages + [{'role': 'assistant', 'content': content or ''},
                            {'role': 'user', 'content': '输出未通过校验：' + last_error + '。请修正并重新输出完整 JSON。'}]
            continue
        return {'trajectory_id': item['trajectory_id'], 'status': 'success', 'model_output': obj,
                'derived': derive(obj, rubric), 'input_stats': stats, 'input_sha256': input_hash,
                'user_query_sha256': item['user_query_sha256'], 'judge': {**config,
                'attempts': attempts, 'usage_total': dict(usage), 'response_id': response_data.get('id'),
                'response_model': response_data.get('model'), 'elapsed_seconds': round(time.monotonic() - start, 3)},
                'completed_at': now()}
    return {'trajectory_id': item['trajectory_id'], 'status': 'error', 'error': last_error,
            'input_sha256': input_hash, 'user_query_sha256': item['user_query_sha256'],
            'judge': {**config, 'attempts': attempts, 'usage_total': dict(usage),
                      'elapsed_seconds': round(time.monotonic() - start, 3)}, 'completed_at': now()}


def grade(candidates, output, *, endpoint, model='glm-5.3', reasoning_effort='high',
          workers=16, api_key=None, limit=None):
    api_key = api_key or os.environ.get('CURATOR_API_KEY')
    if not api_key:
        raise ValueError('set_CURATOR_API_KEY')
    if not endpoint.startswith('https://') or '@' in endpoint or '?' in endpoint:
        raise ValueError('endpoint_requires_https_without_credentials_or_query')
    items = list(read_jsonl(candidates))
    if limit:
        items = items[:limit]
    if len({x['trajectory_id'] for x in items}) != len(items):
        raise ValueError('duplicate_input_ids')
    if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,150}', x['trajectory_id']) for x in items):
        raise ValueError('unsafe_trajectory_id')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'records').mkdir(exist_ok=True)
    prompt = RESOURCES / 'demand_judge_system.md'
    rubric = load_rubric(RESOURCES / 'rubric.json')
    config = {'model': model, 'reasoning_effort': reasoning_effort, 'endpoint': endpoint,
              'temperature': 0, 'max_tokens': 32768, 'runner_version': 'curator-0.1.0',
              'prompt_sha256': file_hash(prompt), 'rubric_sha256': rubric.sha256,
              'upstream_sha256': file_hash(RESOURCES / 'upstream.json')}
    config_hash = digest(dumps(config).encode())
    config['config_sha256'] = config_hash
    config_path = output / 'config.json'
    if config_path.exists() and json.loads(config_path.read_text()) != config:
        raise ValueError('output_configuration_mismatch')
    atomic_json(config_path, config)
    stop = threading.Event()

    def one(item):
        path = output / 'records' / (item['trajectory_id'] + '.json')
        if path.exists():
            old = json.loads(path.read_text())
            if (old['status'] == 'success' and old['user_query_sha256'] == item['user_query_sha256']
                    and old['judge']['config_sha256'] == config_hash):
                return old, True
        result = annotate(item, api_key=api_key, config=config, rubric=rubric,
                          system=prompt.read_text(), stop=stop)
        atomic_json(path, result)
        return result, False

    counts, results, started = Counter(), {}, time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, item) for item in items]
        try:
            for future in as_completed(futures):
                record, cached = future.result()
                results[record['trajectory_id']] = record
                counts[record['status']] += 1
                counts['cache_hits'] += int(cached)
                counts['needs_review'] += int(record.get('model_output', {}).get('needs_review', False))
                progress = {'completed': len(results), 'planned': len(items), **counts,
                            'elapsed_seconds': round(time.monotonic() - started, 1), 'updated_at': now()}
                atomic_json(output / 'progress.json', progress)
                if not cached:
                    print(dumps({'event': 'graded', **progress}), flush=True)
        except FatalEndpointError:
            stop.set()
            for future in futures:
                future.cancel()
            raise
    with (output / 'results.jsonl').open('w') as handle:
        for item in items:
            handle.write(dumps(results[item['trajectory_id']]) + '\n')
    return counts
