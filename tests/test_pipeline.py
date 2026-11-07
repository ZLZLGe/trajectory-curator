import copy
import gzip
import json
from pathlib import Path

import pytest

from trajectory_curator.acquire import scan_shard
from trajectory_curator.common import atomic_json, digest, dumps, file_hash, safe_relative
from trajectory_curator.delivery import export, verify
from trajectory_curator.grading import RESOURCES, annotate, FatalEndpointError, parse_output
from trajectory_curator.screening import assess
from trajectory_curator.selection import choose, deduplicate
from trajectory_curator.vendor.rubric import load_rubric


def raw_record():
    return {'instructions': 'You are a coding assistant.',
            'messages': [{'role': 'system', 'content': 'Do not count this.'},
                         {'role': 'user', 'content': '<environment_context>hidden context</environment_context>\n修复登录接口的空指针异常，并运行单元测试。'},
                         {'role': 'assistant', 'content': 'ASSISTANT_SECRET_SENTINEL'},
                         {'role': 'tool', 'content': 'TOOL_SECRET_SENTINEL'}],
            'metadata': {'session_id': 'session-a', 'sub_session_id': 'not-an-agent-id',
                         'specific_source': 'source', 'user_id': 'user'},
            'completeness': {'is_subagent': False}, 'completeness_tag': 'complete_main_no_sub',
            'normalization_audit': {'reason_codes': []},
            'tool_defs_tag': 'complete', 'tool_call_tag': 'consistent',
            'tool_call_check': {'hallucinated_calls': 0},
            '_basetraj': {'runtime_os': 'linux', 'source_key': 'source-key', 'harness': 'codex',
                         'model': 'example-model', 'context_bucket': '0-50k', 'context_tokens': 1200},
            'total_tool_calls': 1, 'total_rounds': 2}


def output_json():
    return {'demands': [{'id': 'D1', 'summary': '修复登录异常并测试', 'source_turns': ['u0001'],
            'status': 'active', 'deliverable': 'code', 'structure': 'sequential',
            'subtasks': ['修复异常', '运行单元测试'], 'relation': {'type': 'none', 'to': None},
            'difficulty': {'expert_time': 'T1', 'n_steps': 3, 'n_constraints': 1, 'expertise': 1,
                           'reasoning': 1, 'context': 1, 'verification': 1, 'rationale': '常见的单处异常修复，需要测试验证。'}}],
            'primary_demand': 'D1', 'needs_review': False, 'review_reason': ''}


def config():
    return {'model': 'test-model', 'endpoint': 'https://example.invalid/chat/completions',
            'reasoning_effort': 'high', 'max_tokens': 32768,
            'prompt_sha256': file_hash(RESOURCES / 'demand_judge_system.md'),
            'rubric_sha256': file_hash(RESOURCES / 'rubric.json'), 'config_sha256': 'test-config'}


class Response:
    status_code = 200

    def __init__(self, content):
        self.content = content

    def json(self):
        return {'choices': [{'message': {'content': self.content}, 'finish_reason': 'stop'}],
                'model': 'test-model', 'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}


def test_real_users_exclude_environment_tools_agents_and_notifications():
    raw = raw_record()
    raw['messages'] += [
        {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'NOT_A_USER'}]},
        {'role': 'user', 'content': '# AGENTS.md instructions for /tmp\n<INSTRUCTIONS>rules</INSTRUCTIONS>'},
        {'role': 'user', 'content': '<teammate-message teammate_id="a">done</teammate-message>'},
        {'role': 'user', 'content': '继续'}]
    result = assess(raw)
    assert result['eligible']
    assert result['raw_user_messages'] == 5
    assert result['real_user_rounds'] == 2
    assert result['user_turns'][0]['text'] == '修复登录接口的空指针异常，并运行单元测试。'
    assert 'SECRET_SENTINEL' not in dumps(result['user_turns'])


def test_subsession_id_is_not_subagent_and_mentions_are_not_identity():
    raw = raw_record()
    raw['messages'][1]['content'] = '请解释 subagent 与 compact 的区别，并分析这段引用：Another language model started to solve this problem and produced a summary。'
    assert assess(raw)['eligible']
    raw['completeness']['is_subagent'] = True
    assert 'subagent' in assess(raw)['rejection_reasons']


@pytest.mark.parametrize('instructions', [
    'You are a file search specialist for Claude Code.',
    'You are a subagent assigned to inspect files.',
    '<multi_agent_role>You are `/root/worker`.</multi_agent_role>'])
def test_explicit_subagent_instructions(instructions):
    raw = raw_record()
    raw['instructions'] = instructions
    assert 'subagent' in assess(raw)['rejection_reasons']


def test_wrapped_delegation_does_not_become_root():
    raw = raw_record()
    raw['messages'][1]['content'] = '<environment_context>x</environment_context>\n<codex_delegation><input>修复登录接口。</input></codex_delegation>'
    assert 'subagent' in assess(raw)['rejection_reasons']


@pytest.mark.parametrize('change', ['summary', 'metadata', 'items'])
def test_compaction_requires_explicit_boundary(change):
    raw = raw_record()
    if change == 'summary':
        raw['messages'].append({'role': 'user', 'content': 'Another language model started to solve this problem and produced a summary of its thinking process.'})
    elif change == 'metadata':
        raw['metadata']['is_compaction'] = True
    else:
        raw['compaction_items'] = [{'summary': 'earlier context'}]
    assert any(r.startswith('compaction_') for r in assess(raw)['rejection_reasons'])


def test_title_generation_auxiliary_call_is_excluded():
    raw = raw_record()
    raw['instructions'] = 'Generate a concise, sentence-case title (3-7 words) for the conversation.'
    assert 'auxiliary_model_call' in assess(raw)['rejection_reasons']


def test_segments_are_not_additional_user_messages():
    raw = raw_record()
    raw['messages'][1]['content'] = ('# Response annotations:\n<response-annotations>'
        '[{"comment":"修复这处异常"},{"comment":"补上测试用例"}]</response-annotations>')
    result = assess(raw)
    assert result['real_user_rounds'] == 1
    assert result['user_segments'] == 2


def test_incomplete_candidate_never_selected_as_complete():
    raw = raw_record()
    raw['normalization_audit']['reason_codes'] = ['missing_tool_result', 'no_final_assistant_turn']
    raw['completeness_tag'] = 'incomplete_main'
    result = assess(raw)
    assert result['eligible'] and not result['root_complete']
    raw['normalization_audit']['reason_codes'].append('missing_server_tool_id')
    assert not assess(raw)['eligible']


def test_session_or_query_dedup_is_transitive():
    items = []
    for n, (session, query, rounds) in enumerate([('a', 'q1', 1), ('a', 'q2', 3), ('b', 'q2', 2), ('c', 'q3', 1)]):
        items.append({'trajectory_id': str(n), 'session_group': session, 'user_query_sha256': query,
                      'root_complete': True, 'quality_reason_codes': [], 'real_user_rounds': rounds,
                      'total_rounds': rounds, 'total_tool_calls': 1, 'context_tokens': 100,
                      'harness': 'codex', 'context_bucket': '0-50k', 'model': 'model'})
    unique = deduplicate(items)
    assert {i['trajectory_id'] for i in unique} == {'1', '3'}
    assert choose(unique, 2) == choose(list(reversed(unique)), 2)


def test_annotation_sends_only_users_and_corrects_invalid_enum():
    item = {'trajectory_id': 'sample', **assess(raw_record())}
    wrong = output_json()
    wrong['demands'][0]['deliverable'] = 'INVENTED_LABEL'
    replies = iter([dumps(wrong), dumps(output_json())])
    calls = []

    def post(*args, **kwargs):
        calls.append(copy.deepcopy(kwargs['json']))
        return Response(next(replies))

    result = annotate(item, api_key='synthetic-test-key', config=config(),
                      rubric=load_rubric(RESOURCES / 'rubric.json'), system='labeling rules', post=post)
    assert result['status'] == 'success'
    assert len(result['judge']['attempts']) == 2
    assert result['judge']['usage_total']['total_tokens'] == 60
    assert 'SECRET_SENTINEL' not in dumps(calls)
    assert result['input_stats']['truncated_turns'] == []
    assert calls[1]['messages'][-1]['role'] == 'user'
    assert 'INVENTED_LABEL' in calls[1]['messages'][-2]['content']


def test_authentication_failure_is_fatal():
    response = Response('')
    response.status_code = 401
    item = {'trajectory_id': 'sample', **assess(raw_record())}
    with pytest.raises(FatalEndpointError, match='HTTP_401'):
        annotate(item, api_key='synthetic-test-key', config=config(), rubric=load_rubric(RESOURCES / 'rubric.json'),
                 system='rules', post=lambda *a, **kw: response)


def test_strict_json_rejects_duplicate_keys_and_unsafe_paths():
    with pytest.raises(ValueError):
        parse_output('{"demands":[],"demands":[1]}')
    for p in ('../outside', '/absolute', 'a/../b', 'a//b'):
        with pytest.raises(ValueError):
            safe_relative(p)


def test_export_verify_and_tamper_detection(tmp_path):
    raw = raw_record()
    cache = tmp_path / 'cache'
    cache.mkdir()
    line = (dumps(raw) + '\n').encode()
    with gzip.open(cache / 'shard.jsonl.gz', 'wb') as handle:
        handle.write(line)
    entry = {'path': 'shard.jsonl.gz', 'bytes': (cache / 'shard.jsonl.gz').stat().st_size,
             'sha256': file_hash(cache / 'shard.jsonl.gz'), 'rows': 1}
    scanned = scan_shard(entry, cache, tmp_path / 'scan', 'pinned-commit')
    item = json.loads(Path(scanned['candidate_file']).read_text())
    candidates = tmp_path / 'selected.jsonl'
    candidates.write_text(dumps(item) + '\n')
    grades = tmp_path / 'grades'
    (grades / 'records').mkdir(parents=True)
    result = annotate(item, api_key='synthetic-test-key', config=config(),
                      rubric=load_rubric(RESOURCES / 'rubric.json'), system='rules',
                      post=lambda *a, **kw: Response(dumps(output_json())))
    atomic_json(grades / 'records' / (item['trajectory_id'] + '.json'), result)
    atomic_json(grades / 'config.json', config())
    plan = tmp_path / 'plan.json'
    atomic_json(plan, {'shards': [entry]})
    delivery = tmp_path / 'delivery'
    export(candidates, grades, cache, delivery, count=1, plan=plan)
    assert verify(delivery)['passed']
    path = delivery / '轨迹' / (item['trajectory_id'] + '.json')
    assert path.read_bytes() == line
    path.write_bytes(line + b' ')
    with pytest.raises(ValueError, match='original_trajectory_hash_mismatch'):
        verify(delivery)


def test_vendored_files_match_pinned_hashes():
    upstream = json.loads((RESOURCES / 'upstream.json').read_text())
    for item in upstream['files']:
        assert file_hash(RESOURCES.parent / item['local_path']) == item['sha256']
