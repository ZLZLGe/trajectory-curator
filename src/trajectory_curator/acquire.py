"""Pinned HF manifests, bounded-memory shard scanning and auditable candidates."""
import gzip
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import requests

from .common import atomic_json, digest, dumps, file_hash, hf_token, now, read_jsonl, safe_relative
from .screening import assess, VERSION


def download_plan(plan_path, cache, workers=8):
    plan = json.loads(Path(plan_path).read_text())
    token = hf_token()
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    repo = plan.get('repo_id', 'Traj2env/BaseTraj')

    def one(entry):
        relative = safe_relative(entry['path'])
        target = Path(cache) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size == entry['bytes'] and file_hash(target) == entry['sha256']:
            return relative
        url = f"https://huggingface.co/datasets/{repo}/resolve/{plan['revision']}/" + quote(relative, safe='/')
        for attempt in range(5):
            try:
                with requests.get(url, headers=headers, stream=True, timeout=(20, 120)) as response:
                    response.raise_for_status()
                    with target.with_suffix('.tmp').open('wb') as out:
                        for chunk in response.iter_content(2**20):
                            out.write(chunk)
                temporary = target.with_suffix('.tmp')
                if temporary.stat().st_size != entry['bytes'] or file_hash(temporary) != entry['sha256']:
                    raise ValueError('shard_integrity_failure')
                temporary.replace(target)
                return relative
            except Exception:
                if attempt == 4:
                    raise RuntimeError('shard_download_failed:' + relative) from None
                time.sleep(2 ** attempt)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, relative in enumerate(pool.map(one, plan['shards']), 1):
            print(dumps({'event': 'downloaded', 'completed': n, 'total': len(plan['shards']), 'path': relative}), flush=True)


def inventory(repo, revision, platform, output, rows_per_bucket=2500):
    token = hf_token()
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    if revision == 'main':
        response = requests.get('https://huggingface.co/api/datasets/' + repo, headers=headers, timeout=(15, 45))
        response.raise_for_status()
        revision = response.json()['sha']
    url = f'https://huggingface.co/datasets/{repo}/resolve/{revision}/metadata/shards.jsonl'
    response = requests.get(url, headers=headers, timeout=(15, 60))
    response.raise_for_status()
    shards = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    shards = [x for x in shards if x.get('runtime_os') == platform and x['harness'] in ('claude-code', 'codex')]
    groups = defaultdict(list)
    for item in shards:
        groups[item['harness'], item['context_bucket']].append(item)
    selected = []
    for _, group in sorted(groups.items()):
        size = 0
        for item in sorted(group, key=lambda x: digest(('trajectory-curator-v1/' + x['path']).encode())):
            selected.append(item)
            size += item['rows']
            if size >= rows_per_bucket:
                break
    atomic_json(output, {'repo_id': repo, 'revision': revision, 'platform': platform,
                         'manifest_sha256': digest(response.content), 'created_at': now(), 'shards': selected})


def scan_shard(entry, cache, output, revision, repo='Traj2env/BaseTraj'):
    relative = safe_relative(entry['path'])
    target = Path(cache) / relative
    if target.stat().st_size != entry['bytes'] or file_hash(target) != entry['sha256']:
        raise ValueError('downloaded_shard_hash_mismatch')
    dest = Path(output) / 'shards' / (digest(relative.encode()) + '.jsonl')
    audit_path = dest.with_suffix('.audit.jsonl')
    marker = dest.with_suffix('.summary.json')
    identity = {'shard_sha256': entry['sha256'], 'screening_version': VERSION, 'revision': revision, 'repo_id': repo}
    if marker.is_file():
        old = json.loads(marker.read_text())
        if old.get('identity') == identity and file_hash(dest) == old['candidate_sha256'] and file_hash(audit_path) == old['audit_sha256']:
            return old
    dest.parent.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    rows, eligible = 0, 0
    with gzip.open(target, 'rb') as source, dest.with_suffix('.tmp').open('w') as out, audit_path.with_suffix('.tmp').open('w') as audit_out:
        for rows, line in enumerate(source, 1):
            record = json.loads(line)
            assessment = assess(record)
            line_hash = digest(line)
            source_key = record.get('_basetraj', {}).get('source_key', '')
            identifier = 'basetraj_' + digest((source_key or relative + ':' + str(rows)).encode())
            locator = {'repo_id': repo, 'revision': revision, 'shard_path': relative,
                       'row_in_shard': rows, 'original_line_sha256': line_hash, 'source_key': source_key}
            audit_out.write(dumps({'trajectory_id': identifier, 'source': locator,
                                  **{k: v for k, v in assessment.items() if k not in ('user_turns',)}}) + '\n')
            counts.update(assessment['rejection_reasons'])
            if not assessment['eligible']:
                continue
            eligible += 1
            candidate = {'trajectory_id': identifier, 'source': locator, **assessment,
                         'metadata': record.get('metadata', {}),
                         'harness': record['_basetraj']['harness'], 'model': record['_basetraj']['model'],
                         'context_bucket': record['_basetraj']['context_bucket'],
                         'context_tokens': record['_basetraj']['context_tokens'],
                         'total_tool_calls': record.get('total_tool_calls', 0),
                         'total_rounds': record.get('total_rounds', 0),
                         'scenario_labels': record.get('classification', {}).get('scenario_labels', []),
                         'subagent_children': len(record.get('sub_agent_trajectory') or {})}
            out.write(dumps(candidate) + '\n')
    if rows != entry['rows']:
        raise ValueError('shard_row_count_mismatch')
    dest.with_suffix('.tmp').replace(dest)
    audit_path.with_suffix('.tmp').replace(audit_path)
    result = {'identity': identity, 'path': relative, 'candidate_file': str(dest), 'audit_file': str(audit_path),
              'rows': rows, 'eligible': eligible, 'rejection_counts': dict(counts),
              'candidate_sha256': file_hash(dest), 'audit_sha256': file_hash(audit_path)}
    atomic_json(marker, result)
    return result


def screen(plan_path, cache, output, workers=4):
    plan = json.loads(Path(plan_path).read_text())
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(scan_shard, item, str(cache), str(output), plan['revision'],
                               plan.get('repo_id', 'Traj2env/BaseTraj')) for item in plan['shards']]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            atomic_json(output / 'progress.json', {'complete': len(results) == len(futures),
                        'completed_shards': len(results), 'planned_shards': len(futures),
                        'rows': sum(x['rows'] for x in results), 'eligible': sum(x['eligible'] for x in results),
                        'updated_at': now(), 'shards': results})
            print(dumps({'event': 'screened', 'completed': len(results), 'total': len(futures),
                         'rows': result['rows'], 'eligible': result['eligible'], 'path': result['path']}), flush=True)
    with (output / 'candidates.jsonl').open('w') as out:
        for result in sorted(results, key=lambda x: x['path']):
            with Path(result['candidate_file']).open() as handle:
                for line in handle:
                    out.write(line)
    rejections = Counter()
    for result in results:
        rejections.update(result['rejection_counts'])
    atomic_json(output / 'summary.json', {'screening_version': VERSION, 'revision': plan['revision'],
                'shards': len(results), 'rows': sum(x['rows'] for x in results),
                'eligible': sum(x['eligible'] for x in results), 'rejection_counts': dict(rejections),
                'candidates_sha256': file_hash(output / 'candidates.jsonl')})
