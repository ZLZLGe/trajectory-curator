import copy
import gzip
import json
from pathlib import Path

import pytest

from trajectory_curator.acquire import scan_shard
from trajectory_curator.common import atomic_json, dumps, file_hash, safe_relative
from trajectory_curator.delivery import accepted_result, export, verify
from trajectory_curator.grading import RESOURCES, annotate, FatalEndpointError, grade, parse_output, RequestGate
from trajectory_curator.screening import assess, observed_tool_calls
from trajectory_curator.selection import choose, deduplicate, select
from trajectory_curator.vendor.rubric import load_rubric


def raw_record():
    return {'instructions': 'You are a coding assistant.',
            'messages': [{'role': 'system', 'content': 'Do not count this.'},
                         {'role': 'user', 'content': '<environment_context>hidden context</environment_context>\n修复登录接口的空指针异常，并运行单元测试。'},
                         {'role': 'assistant', 'content': 'ASSISTANT_SECRET_SENTINEL',
                          'tool_calls': [{'id': 'call_1', 'type': 'function',
                                          'function': {'name': 'run_tests', 'arguments': '{}'}}]},
                         {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'TOOL_SECRET_SENTINEL'}],
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


@pytest.mark.parametrize('metadata, rejected', [
    ({'compaction': {'confirmed': False}}, False),
    ({'compaction': False}, False),
    ({'compaction': {}}, False),
    ({'compaction': {'confirmed': 'false'}}, False),
    ({'is_compaction': False, 'epoch_cause': 'system_change', 'epoch_index': 1}, False),
    ({'compaction': True}, True),
    ({'compaction': {'confirmed': True}}, True),
    ({'compaction': {'confirmed': False}, 'is_compaction': True}, True),
    ({'compaction': {'confirmed': False}, 'epoch_cause': 'compaction'}, True),
])
def test_compaction_metadata_requires_positive_confirmation(metadata, rejected):
    raw = raw_record()
    raw['metadata'].update(metadata)
    result = assess(raw)
    assert result['eligible'] is not rejected
    assert ('explicit_compaction_metadata' in result['identity_evidence']) is rejected


@pytest.mark.parametrize('confirmed', [False, True])
def test_child_compaction_metadata_uses_same_confirmation_rule(confirmed):
    raw = raw_record()
    raw['completeness_tag'] = 'complete_main_with_complete_sub'
    child = raw_record()
    child['completeness']['is_subagent'] = True
    child['metadata']['compaction'] = {'confirmed': confirmed}
    raw['sub_agent_trajectory'] = {'child': child}
    result = assess(raw)
    assert result['eligible'] is not confirmed
    assert not result['is_subagent']


def test_unconfirmed_metadata_does_not_override_continuation_summary():
    raw = raw_record()
    raw['metadata']['compaction'] = {'confirmed': False}
    raw['messages'].append({'role': 'user', 'content':
        'This session is being continued from a previous conversation that ran out of context. Summary: prior work'})
    assert 'compaction_history_incomplete' in assess(raw)['rejection_reasons']


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


def test_suggestion_mode_and_local_commands_are_not_real_users():
    raw = raw_record()
    raw['messages'] += [
        {'role': 'user', 'content': '[SUGGESTION MODE: Suggest what the user might naturally type next into Claude Code.]\nPredict the next text.'},
        {'role': 'user', 'content': '<command-name>/model</command-name><command-message>model</command-message><command-args>example</command-args><local-command-stdout>Set model</local-command-stdout>'}]
    result = assess(raw)
    assert result['real_user_rounds'] == 1
    assert result['extraction_audit']['excluded_suggestion_mode'] == 1
    assert result['extraction_audit']['removed_cli_exporter_blocks'] == 4


def test_cli_goal_retains_task_and_clear_retains_following_request():
    raw = raw_record()
    raw['messages'][1]['content'] = '<command-name>/goal</command-name><command-message>goal</command-message><command-args>修复登录接口并测试</command-args>'
    assert assess(raw)['user_turns'][0]['text'] == '修复登录接口并测试'


def test_claude_naming_prompt_is_auxiliary_even_after_generic_main_identity():
    raw = raw_record()
    raw['instructions'] = "You are Claude Code, Anthropic's official CLI for Claude.You are naming a coding session so the user can pick it out."
    assert 'auxiliary_model_call' in assess(raw)['rejection_reasons']


@pytest.mark.parametrize('text', [
    '[SYSTEM NOTIFICATION - NOT USER INPUT]\nNo human input has been received.',
    'Base directory for this skill: /tmp/skills/example\n# Injected skill instructions',
    '[Request interrupted by user]\n<local-command-caveat>not a user request</local-command-caveat>',
    '<bash-input>pwd</bash-input><bash-stdout>/tmp</bash-stdout><bash-stderr></bash-stderr>'])
def test_caller_framework_messages_do_not_inflate_user_rounds(text):
    raw = raw_record()
    raw['messages'].append({'role': 'user', 'content': text})
    assert assess(raw)['real_user_rounds'] == 1


def test_interrupt_and_ide_context_removal_preserves_real_request():
    raw = raw_record()
    raw['messages'][1]['content'] = ('[Request interrupted by user for tool use]\n'
        '<ide_opened_file>The user opened a file.</ide_opened_file>修复登录接口并测试')
    result = assess(raw)
    assert result['real_user_rounds'] == 1
    assert result['user_turns'][0]['text'] == '修复登录接口并测试'
    assert result['extraction_audit']['removed_interrupt_markers'] == 1
    raw['messages'][1]['content'] = '<command-name>/clear</command-name><command-args></command-args><local-command-stdout></local-command-stdout>修复登录接口并测试'
    assert assess(raw)['user_turns'][0]['text'] == '修复登录接口并测试'


def test_incomplete_candidate_never_selected_as_complete():
    raw = raw_record()
    raw['normalization_audit']['reason_codes'] = ['missing_tool_result', 'no_final_assistant_turn']
    raw['completeness_tag'] = 'incomplete_main'
    result = assess(raw)
    assert result['eligible'] and not result['root_complete']
    raw['normalization_audit']['reason_codes'].append('missing_server_tool_id')
    assert not assess(raw)['eligible']


@pytest.mark.parametrize('case', ['none', 'definitions_only', 'summary_only', 'child_only', 'quoted_only', 'invalid_summary'])
def test_delivery_requires_actual_root_tool_calls(case):
    raw = raw_record()
    raw['messages'][2].pop('tool_calls')
    raw['total_tool_calls'] = 0
    if case == 'definitions_only':
        raw['tools'] = [{'type': 'function', 'function': {'name': 'run_tests'}}]
    elif case == 'summary_only':
        raw['total_tool_calls'] = 10
    elif case == 'child_only':
        raw['total_tool_calls'] = 1
        raw['sub_agent_trajectory'] = {'worker': raw_record()}
    elif case == 'quoted_only':
        raw['messages'][1]['content'] += '\nExample: {"tool_calls":[{"function":{"name":"run_tests"}}]}'
    elif case == 'invalid_summary':
        raw['messages'][2]['tool_calls'] = raw_record()['messages'][2]['tool_calls']
        raw['total_tool_calls'] = True
    assert 'no_actual_tool_calls' in assess(raw)['rejection_reasons']


def test_actual_tool_calls_support_normalized_and_anthropic_messages():
    raw = raw_record()
    assert observed_tool_calls(raw) == 1
    raw['messages'][2].pop('tool_calls')
    raw['messages'][2]['content'] = [{'type': 'tool_use', 'name': 'run_tests', 'input': {}}]
    assert assess(raw)['eligible']
    assert observed_tool_calls(raw) == 1


def test_malformed_tool_structures_do_not_count_as_invocations():
    raw = raw_record()
    raw['messages'][2]['tool_calls'] = [None, {'function': 'run_tests'}, {'function': {'name': ''}}]
    assert observed_tool_calls(raw) == 0
    assert 'no_actual_tool_calls' in assess(raw)['rejection_reasons']


def test_select_rejects_old_candidates_without_observed_tool_calls(tmp_path):
    raw = raw_record()
    item = {'trajectory_id': 'has-tools', **assess(raw), 'total_tool_calls': 1,
            'total_rounds': 2, 'context_tokens': 100, 'harness': 'codex', 'context_bucket': '0-50k', 'model': 'model'}
    stale = {**item, 'trajectory_id': 'stale', 'session_group': 'other', 'user_query_sha256': 'other',
             'observed_tool_calls': 0}
    path = tmp_path / 'candidates.jsonl'
    path.write_text(dumps(stale) + '\n' + dumps(item) + '\n')
    assert [x['trajectory_id'] for x in select(path, tmp_path / 'selected.jsonl', count=1)] == ['has-tools']


def test_session_or_query_dedup_is_transitive():
    items = []
    for n, (session, query, rounds) in enumerate([('a', 'q1', 1), ('a', 'q2', 3), ('b', 'q2', 2), ('c', 'q3', 1)]):
        items.append({'trajectory_id': str(n), 'session_group': session, 'user_query_sha256': query,
                      'root_complete': True, 'quality_reason_codes': [], 'real_user_rounds': rounds,
                      'total_rounds': rounds, 'total_tool_calls': 1, 'context_tokens': 100,
                      'harness': 'codex', 'context_bucket': '0-50k', 'model': 'model'})
    unique = deduplicate(items)
    assert {i['trajectory_id'] for i in unique} == {'1', '3'}
    assert {i['trajectory_id'] for i in deduplicate(items, preferred_ids={'2'})} == {'2', '3'}
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


def annotate_turns(texts, output=None):
    raw = raw_record()
    raw['messages'].extend({'role': 'user', 'content': text} for text in texts)
    item = {'trajectory_id': 'sample', **assess(raw)}
    result = annotate(item, api_key='synthetic-test-key', config=config(),
                      rubric=load_rubric(RESOURCES / 'rubric.json'), system='rules',
                      post=lambda *a, **kw: Response(dumps(output if output is not None else output_json())))
    return item, result


@pytest.mark.parametrize('text', ['另一个独立任务：撰写一份完整的产品市场分析报告。', '加测试', '继续修复支付接口'])
def test_unreferenced_request_requires_review_and_cannot_be_delivered(text):
    item, result = annotate_turns([text])
    assert result['status'] == 'success'
    assert result['model_output']['needs_review']
    assert 'u0002' in result['model_output']['review_reason']
    assert result['coverage_audit']['uncovered_turn_ids'] == ['u0002']
    assert not accepted_result(item, result)


@pytest.mark.parametrize('reply', ['好的。', '继续', 'A', '谢谢！', 'OK.', '进度如何？'])
def test_known_non_demand_reply_does_not_require_a_demand_reference(reply):
    item, result = annotate_turns([reply])
    assert not result['model_output']['needs_review']
    assert result['coverage_audit']['exempt_turn_ids'] == ['u0002']
    assert accepted_result(item, result)


@pytest.mark.parametrize('referenced_id', ['u0001', 'u0002'])
def test_collapsed_consecutive_duplicates_share_coverage(referenced_id):
    output = output_json()
    output['demands'][0]['source_turns'] = [referenced_id]
    item, result = annotate_turns(['修复登录接口的空指针异常，并运行单元测试。'], output)
    assert result['input_stats']['duplicate_turns_merged'] == 1
    assert result['coverage_audit']['uncovered_turn_ids'] == []
    assert accepted_result(item, result)


def test_nonconsecutive_repeated_request_still_requires_a_reference():
    output = output_json()
    output['demands'][0]['source_turns'].append('u0002')
    _, result = annotate_turns(['先取消这个修复任务。', '修复登录接口的空指针异常，并运行单元测试。'], output)
    assert result['coverage_audit']['uncovered_turn_ids'] == ['u0003']
    assert result['model_output']['needs_review']


def test_cancelled_demand_counts_as_accounted_for():
    output = output_json()
    output['demands'].append({**copy.deepcopy(output['demands'][0]), 'id': 'D2',
        'summary': '已取消的市场报告', 'source_turns': ['u0002'], 'status': 'cancelled',
        'relation': {'type': 'independent', 'to': None}, 'difficulty': None})
    item, result = annotate_turns(['原先提过的市场报告取消，不做了。'], output)
    assert accepted_result(item, result)


def test_coverage_review_preserves_model_review_reason():
    output = output_json()
    output.update(needs_review=True, review_reason='需求边界需人工确认')
    _, result = annotate_turns(['加测试'], output)
    assert result['model_output']['review_reason'].startswith('需求边界需人工确认; ')
    assert 'u0002' in result['model_output']['review_reason']


def test_cached_legacy_annotation_is_rechecked_without_model_calls(tmp_path, monkeypatch):
    item, _ = annotate_turns(['另一个独立任务：撰写完整的市场分析报告。'])
    candidates = tmp_path / 'candidates.jsonl'
    candidates.write_text(dumps(item) + '\n')
    output = tmp_path / 'grades'
    monkeypatch.setattr('trajectory_curator.grading.requests.post', lambda *a, **kw: Response(dumps(output_json())))
    kwargs = {'endpoint': 'https://example.invalid/chat/completions', 'api_key': 'synthetic-test-key'}
    assert grade(candidates, output, **kwargs)['needs_review'] == 1
    path = output / 'records/sample.json'
    cached = json.loads(path.read_text())
    cached.pop('coverage_audit')
    cached['model_output'].update(needs_review=False, review_reason='')
    atomic_json(path, cached)

    def no_request(*args, **kwargs):
        pytest.fail('cached annotations must be checked locally')

    monkeypatch.setattr('trajectory_curator.grading.requests.post', no_request)
    counts = grade(candidates, output, **kwargs)
    assert counts['cache_hits'] == 1 and counts['needs_review'] == 1
    checked = json.loads(path.read_text())
    assert checked['coverage_audit']['uncovered_turn_ids'] == ['u0002']
    assert not accepted_result(item, checked)
    grade(candidates, output, **kwargs)
    assert json.loads(path.read_text()) == checked


def test_export_rechecks_coverage_of_legacy_results(tmp_path):
    item, result = annotate_turns(['另一个独立任务：撰写完整的市场分析报告。'])
    item.update(total_tool_calls=1)
    result.pop('coverage_audit')
    result['model_output'].update(needs_review=False, review_reason='')
    candidates = tmp_path / 'candidates.jsonl'
    candidates.write_text(dumps(item) + '\n')
    grades = tmp_path / 'grades'
    atomic_json(grades / 'records/sample.json', result)
    with pytest.raises(ValueError, match='insufficient_accepted_annotations:0<1'):
        export(candidates, grades, tmp_path / 'cache', tmp_path / 'delivery', count=1)


def test_authentication_failure_is_fatal():
    response = Response('')
    response.status_code = 401
    item = {'trajectory_id': 'sample', **assess(raw_record())}
    with pytest.raises(FatalEndpointError, match='HTTP_401'):
        annotate(item, api_key='synthetic-test-key', config=config(), rubric=load_rubric(RESOURCES / 'rubric.json'),
                 system='rules', post=lambda *a, **kw: response)


def test_request_gate_spaces_all_attempts(monkeypatch):
    sleeps = []
    monkeypatch.setattr('trajectory_curator.grading.time.monotonic', lambda: 100)
    monkeypatch.setattr('trajectory_curator.grading.time.sleep', sleeps.append)
    gate = RequestGate(60)
    for _ in range(3):
        gate.wait()
    assert sleeps == [1, 2]


def test_rate_limit_honors_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr('trajectory_curator.grading.time.sleep', sleeps.append)
    limited = Response('')
    limited.status_code = 429
    limited.headers = {'Retry-After': '30'}
    responses = iter([limited, Response(dumps(output_json()))])
    item = {'trajectory_id': 'sample', **assess(raw_record())}
    result = annotate(item, api_key='synthetic-test-key', config=config(), rubric=load_rubric(RESOURCES / 'rubric.json'),
                      system='rules', post=lambda *a, **kw: next(responses))
    assert result['status'] == 'success'
    assert sleeps == [30]


def test_strict_json_rejects_duplicate_keys_and_unsafe_paths():
    with pytest.raises(ValueError):
        parse_output('{"demands":[],"demands":[1]}')
    for p in ('../outside', '/absolute', 'a/../b', 'a//b'):
        with pytest.raises(ValueError):
            safe_relative(p)


@pytest.mark.parametrize('legacy_missing_turn', [False, True])
def test_export_verify_and_tamper_detection(tmp_path, legacy_missing_turn):
    raw = raw_record()
    if legacy_missing_turn:
        raw['messages'].append({'role': 'user', 'content': '新增一个独立的报表导出功能。'})
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
    stale = {**item, 'trajectory_id': 'old-no-tools', 'total_tool_calls': 0, 'observed_tool_calls': 0}
    candidates.write_text(dumps(stale) + '\n' + dumps(item) + '\n')
    grades = tmp_path / 'grades'
    (grades / 'records').mkdir(parents=True)
    model_output = output_json()
    if legacy_missing_turn:
        model_output['demands'][0]['source_turns'].append('u0002')
    result = annotate(item, api_key='synthetic-test-key', config=config(),
                      rubric=load_rubric(RESOURCES / 'rubric.json'), system='rules',
                      post=lambda *a, **kw: Response(dumps(model_output)))
    atomic_json(grades / 'records' / (item['trajectory_id'] + '.json'), result)
    atomic_json(grades / 'config.json', config())
    plan = tmp_path / 'plan.json'
    atomic_json(plan, {'shards': [entry]})
    delivery = tmp_path / 'delivery'
    counts = export(candidates, grades, cache, delivery, count=1, plan=plan)
    assert counts['annotation_exclusions']['no_actual_tool_calls'] == 1
    assert counts['without_tool_calls'] == 0
    assert verify(delivery)['passed']
    if legacy_missing_turn:
        annotation_path = delivery / '难度分级.jsonl'
        annotation = json.loads(annotation_path.read_text())
        annotation.pop('coverage_audit')
        annotation['model_output']['demands'][0]['source_turns'] = ['u0001']
        annotation_path.write_text(dumps(annotation) + '\n')
        manifest_path = delivery / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['难度分级.jsonl'] = {
            'sha256': file_hash(annotation_path), 'bytes': annotation_path.stat().st_size}
        atomic_json(manifest_path, manifest)
        with pytest.raises(ValueError, match='annotation_not_accepted'):
            verify(delivery)
        return
    path = delivery / '轨迹' / (item['trajectory_id'] + '.json')
    assert path.read_bytes() == line
    path.write_bytes(line + b' ')
    with pytest.raises(ValueError, match='original_trajectory_hash_mismatch'):
        verify(delivery)


def test_scan_recomputes_old_compaction_rejections(tmp_path):
    raw = raw_record()
    raw['metadata']['compaction'] = {'confirmed': False}
    shard = tmp_path / 'shard.jsonl.gz'
    with gzip.open(shard, 'wb') as handle:
        handle.write((dumps(raw) + '\n').encode())
    entry = {'path': shard.name, 'bytes': shard.stat().st_size, 'sha256': file_hash(shard), 'rows': 1}
    first = scan_shard(entry, tmp_path, tmp_path / 'scan', 'pinned-commit')
    assert first['eligible'] == 1
    candidate = Path(first['candidate_file'])
    candidate.write_text('')
    marker = candidate.with_suffix('.summary.json')
    legacy = {**first, 'eligible': 0, 'candidate_sha256': file_hash(candidate),
              'identity': {**first['identity'], 'screening_version': 'screening-v5-tool-required'}}
    atomic_json(marker, legacy)
    rescanned = scan_shard(entry, tmp_path, tmp_path / 'scan', 'pinned-commit')
    assert rescanned['eligible'] == 1
    assert json.loads(candidate.read_text())['compaction_status'] == 'no_observed_boundary'


def test_vendored_files_match_pinned_hashes():
    upstream = json.loads((RESOURCES / 'upstream.json').read_text())
    for item in upstream['files']:
        assert file_hash(RESOURCES.parent / item['local_path']) == item['sha256']
