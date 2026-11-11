"""Export original source lines and independently verify every delivered annotation."""
import csv
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path

from .common import atomic_json, digest, dumps, file_hash, now, read_jsonl, safe_relative
from .coverage import assess_coverage
from .grading import RESOURCES
from .screening import assess
from .vendor.inputs import build_payload, normalize_item
from .vendor.rubric import load_rubric
from .vendor.scoring import derive
from .vendor.validation import validate_output


def accepted_result(item, record):
    return (record.get('status') == 'success' and not record['model_output']['needs_review']
            and record['derived']['n_demands'] > 0
            and item['user_query_sha256'] == record['user_query_sha256']
            and not assess_coverage(item['user_turns'], record['model_output'])['uncovered_turn_ids'])


def export(candidates, grades, cache, output, count=1500, plan=None):
    items = list(read_jsonl(candidates))
    chosen, records, excluded = [], {}, Counter()
    for item in items:
        if item.get('total_tool_calls', 0) < 1 or item.get('observed_tool_calls', 0) < 1:
            excluded['no_actual_tool_calls'] += 1
            continue
        p = Path(grades) / 'records' / (item['trajectory_id'] + '.json')
        if not p.exists():
            excluded['not_annotated'] += 1
            continue
        record = json.loads(p.read_text())
        if not accepted_result(item, record):
            excluded['annotation_error_or_review_required'] += 1
            continue
        chosen.append(item)
        records[item['trajectory_id']] = record
    # Preserve the deterministic stratified selection order, independent of API completion order.
    if len(chosen) < count:
        raise ValueError(f'insufficient_accepted_annotations:{len(chosen)}<{count}')
    available = len(chosen)
    chosen = chosen[:count]
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    raw_dir = output / '轨迹'
    raw_dir.mkdir(exist_ok=True)
    expected_names = {item['trajectory_id'] + '.json' for item in chosen}
    if {p.name for p in raw_dir.iterdir()} - expected_names:
        raise ValueError('output_contains_unexpected_trajectory_files')
    groups = defaultdict(dict)
    for item in chosen:
        source = item['source']
        groups[source['shard_path']][source['row_in_shard']] = item
    source_rows = {}
    source_hashes = {}
    if plan:
        plan_data = json.loads(Path(plan).read_text())
        source_hashes = {e['path']: e['sha256'] for e in plan_data['shards']}
    for shard, rows in sorted(groups.items()):
        shard_file = Path(cache) / safe_relative(shard)
        if shard in source_hashes and file_hash(shard_file) != source_hashes[shard]:
            raise ValueError('source_shard_hash_changed')
        wanted = set(rows)
        with gzip.open(shard_file, 'rb') as handle:
            for n, line in enumerate(handle, 1):
                if n not in wanted:
                    continue
                item = rows[n]
                if digest(line) != item['source']['original_line_sha256']:
                    raise ValueError('source_line_hash_changed')
                (raw_dir / (item['trajectory_id'] + '.json')).write_bytes(line)
                source_rows[item['trajectory_id']] = {'trajectory_id': item['trajectory_id'],
                    'source': item['source'], 'source_shard_sha256': source_hashes.get(shard),
                    'relative_file': '轨迹/' + item['trajectory_id'] + '.json',
                    'file_sha256': digest(line), 'file_bytes': len(line)}
                wanted.remove(n)
                if not wanted:
                    break
        if wanted:
            raise ValueError('source_rows_missing')
    flattened = []
    with (output / '真实用户轮次.jsonl').open('w') as users, (output / '难度分级.jsonl').open('w') as annotations, \
            (output / '来源清单.jsonl').open('w') as sources:
        for item in chosen:
            tid = item['trajectory_id']
            result = records[tid]
            active = [d for d in result['model_output']['demands'] if d['status'] == 'active']
            total_subtasks = sum(len(d['subtasks']) for d in active)
            primary = next(d for d in active if d['id'] == result['derived']['primary_demand'])
            scores = {d['id']: d['scoring']['difficulty_score'] for d in result['derived']['demands']
                      if d['status'] == 'active'}
            hardest = max(active, key=lambda d: scores[d['id']])
            annotations.write(dumps({**result, 'real_user_rounds': item['real_user_rounds'],
                        'active_subtask_count': total_subtasks, 'primary_subtask_count': len(primary['subtasks']),
                        'total_tool_calls': item['total_tool_calls'], 'observed_tool_calls': item['observed_tool_calls'],
                        'source': item['source'], 'harness': item['harness'], 'source_model': item['model']}) + '\n')
            users.write(dumps({k: item[k] for k in ('trajectory_id', 'source', 'user_turns', 'real_user_rounds',
                        'raw_user_messages', 'user_segments', 'user_query_characters', 'extraction_audit',
                        'user_query_sha256', 'is_subagent', 'identity_evidence', 'compaction_status',
                        'screening_version', 'root_complete', 'quality_reason_codes', 'session_group',
                        'observed_tool_calls')}) + '\n')
            sources.write(dumps(source_rows[tid]) + '\n')
            flattened.append({'轨迹ID': tid, '轨迹文件': source_rows[tid]['relative_file'],
                    '真实用户轮数': item['real_user_rounds'], '原始user消息数': item['raw_user_messages'],
                    '工具调用数': item['total_tool_calls'], '原始total_rounds': item['total_rounds'],
                    '根轨迹实际工具调用数': item['observed_tool_calls'],
                    '有效需求数': result['derived']['n_demands'], '内部子任务总数': total_subtasks,
                    '主需求子任务数': len(primary['subtasks']),
                    '难度等级': result['derived']['max_difficulty_level'],
                    '难度分数': result['derived']['max_difficulty_score'],
                    '主需求难度等级': result['derived']['primary_difficulty_level'],
                    '主需求难度分数': result['derived']['primary_difficulty_score'],
                    '主需求': primary['summary'], '最难需求': hardest['summary'],
                    '难度依据': hardest['difficulty']['rationale'], '主需求难度依据': primary['difficulty']['rationale'],
                    '需求摘要': '；'.join(d['summary'] for d in active),
                    '子任务': dumps({d['id']: d['subtasks'] for d in active}),
                    'harness': item['harness'], '原模型': item['model'], '上下文桶': item['context_bucket'],
                    '是否子代理': False, 'compact状态': item['compaction_status'],
                    '标注模型': result['judge']['model']})
    with (output / '难度分级.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(flattened[0]))
        writer.writeheader()
        # CSV formula injection protection, while JSONL preserves exact model text.
        for row in flattened:
            writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v
                             for k, v in row.items()})
    counts = {'delivered': len(chosen), 'accepted_annotations_available': available,
              'annotation_exclusions': dict(excluded),
              'harness': dict(Counter(x['harness'] for x in chosen)),
              'source_model': dict(Counter(x['model'] for x in chosen)),
              'context_bucket': dict(Counter(x['context_bucket'] for x in chosen)),
              'real_user_rounds': dict(Counter(x['real_user_rounds'] for x in chosen)),
              'max_difficulty_level': dict(Counter(row['难度等级'] for row in flattened)),
              'single_user_round': sum(i['real_user_rounds'] == 1 for i in chosen),
              'multi_user_round': sum(i['real_user_rounds'] > 1 for i in chosen),
              'total_real_user_rounds': sum(i['real_user_rounds'] for i in chosen),
              'with_tool_calls': sum(i['total_tool_calls'] > 0 for i in chosen),
              'without_tool_calls': sum(i['total_tool_calls'] == 0 for i in chosen),
              'total_active_demands': sum(row['有效需求数'] for row in flattened),
              'total_active_subtasks': sum(row['内部子任务总数'] for row in flattened)}
    files = {name: {'sha256': file_hash(output / name), 'bytes': (output / name).stat().st_size}
             for name in ('难度分级.csv', '难度分级.jsonl', '真实用户轮次.jsonl', '来源清单.jsonl')}
    atomic_json(output / 'manifest.json', {'schema_version': 1, 'created_at': now(), **counts, 'files': files,
                'screening_policy': {'minimum_total_tool_calls': 1, 'minimum_observed_root_tool_calls': 1},
                'source_revision': chosen[0]['source']['revision'], 'candidate_sha256': file_hash(candidates),
                'judge_config': json.loads((Path(grades) / 'config.json').read_text()),
                'reference_versions': json.loads((RESOURCES / 'upstream.json').read_text())})
    return counts


def verify(output):
    output = Path(output)
    manifest = json.loads((output / 'manifest.json').read_text())
    for name, spec in manifest['files'].items():
        if file_hash(output / safe_relative(name)) != spec['sha256']:
            raise ValueError('delivery_file_hash_mismatch:' + name)
    users_list = list(read_jsonl(output / '真实用户轮次.jsonl'))
    grades_list = list(read_jsonl(output / '难度分级.jsonl'))
    sources_list = list(read_jsonl(output / '来源清单.jsonl'))
    by_id = [{x['trajectory_id']: x for x in rows} for rows in (users_list, grades_list, sources_list)]
    users, grades, sources = by_id
    expected = manifest['delivered']
    if any(len(rows) != expected or len(index) != expected for rows, index in zip((users_list, grades_list, sources_list), by_id)):
        raise ValueError('row_count_or_duplicate_ids')
    if not users.keys() == grades.keys() == sources.keys():
        raise ValueError('sidecar_id_mismatch')
    if len({u['session_group'] for u in users.values()}) != expected or len({u['user_query_sha256'] for u in users.values()}) != expected:
        raise ValueError('duplicate_session_or_query')
    if {p.name for p in (output / '轨迹').iterdir()} != {s['relative_file'].split('/')[-1] for s in sources.values()}:
        raise ValueError('unexpected_or_missing_trajectory_files')
    rubric = load_rubric(RESOURCES / 'rubric.json')
    for tid, user in users.items():
        source, result = sources[tid], grades[tid]
        path = output / safe_relative(source['relative_file'])
        if file_hash(path) != source['file_sha256'] or source['file_sha256'] != source['source']['original_line_sha256']:
            raise ValueError('original_trajectory_hash_mismatch')
        raw = json.loads(path.read_bytes())
        if raw['_basetraj']['source_key'] != source['source']['source_key']:
            raise ValueError('source_identity_mismatch')
        again = assess(raw)
        if not again['eligible'] or not again['root_complete'] or again['quality_reason_codes']:
            raise ValueError('delivery_screening_failed')
        for key in ('real_user_rounds', 'user_turns', 'user_query_sha256', 'session_group', 'screening_version',
                    'observed_tool_calls'):
            if again[key] != user[key]:
                raise ValueError('user_extraction_mismatch:' + key)
        if not accepted_result(user, result) or result['real_user_rounds'] != user['real_user_rounds']:
            raise ValueError('annotation_not_accepted')
        if result['observed_tool_calls'] != again['observed_tool_calls'] or result['total_tool_calls'] != raw['total_tool_calls']:
            raise ValueError('tool_count_mismatch')
        validate_output(result['model_output'], [t['turn_id'] for t in user['user_turns']], rubric)
        if derive(result['model_output'], rubric) != result['derived']:
            raise ValueError('difficulty_recalculation_mismatch')
        n = sum(len(d['subtasks']) for d in result['model_output']['demands'] if d['status'] == 'active')
        if n != result['active_subtask_count']:
            raise ValueError('subtask_count_mismatch')
        payload, stats = build_payload(normalize_item(user), turn_cap=120000, total_budget=120000)
        if digest(dumps(payload).encode()) != result['input_sha256'] or stats != result['input_stats']:
            raise ValueError('model_input_hash_mismatch')
        if stats['truncated_turns'] or stats['budget_exceeded']:
            raise ValueError('truncated_model_input')
        if (result['judge']['prompt_sha256'] != file_hash(RESOURCES / 'demand_judge_system.md')
                or result['judge']['rubric_sha256'] != rubric.sha256
                or result['judge']['config_sha256'] != manifest['judge_config']['config_sha256']):
            raise ValueError('prompt_or_rubric_mismatch')
    with (output / '难度分级.csv').open(encoding='utf-8-sig', newline='') as handle:
        csv_rows = list(csv.DictReader(handle))
    if len(csv_rows) != expected or {r['轨迹ID'] for r in csv_rows} != users.keys():
        raise ValueError('csv_count_mismatch')
    for row in csv_rows:
        result, user = grades[row['轨迹ID']], users[row['轨迹ID']]
        for key, value in {'真实用户轮数': user['real_user_rounds'], '有效需求数': result['derived']['n_demands'],
                           '内部子任务总数': result['active_subtask_count'],
                           '难度等级': result['derived']['max_difficulty_level'],
                           '难度分数': result['derived']['max_difficulty_score'],
                           '工具调用数': result['total_tool_calls'],
                           '根轨迹实际工具调用数': result['observed_tool_calls']}.items():
            if str(value) != row[key]:
                raise ValueError('csv_value_mismatch:' + key)
    report = {'passed': True, 'verified_trajectories': expected, 'verified_at': now(),
              'checks': ['source_line_sha256', 'sidecar_ids', 'session_and_query_deduplication',
                         'subagent_compaction_and_quality_rescreen', 'real_user_turn_reextraction',
                         'model_json_schema', 'user_turn_demand_coverage', 'difficulty_recalculation', 'subtask_counts',
                         'full_user_input_hash', 'pinned_prompt_and_rubric', 'csv_integrity',
                         'every_trajectory_has_actual_root_tool_calls']}
    atomic_json(output / '校验报告.json', report)
    return report
