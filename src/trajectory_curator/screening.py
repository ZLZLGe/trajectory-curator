"""Conservative checks based on captured evidence, never directory names alone."""
import json
import re
from collections import Counter

from .common import digest, dumps
from .vendor.extraction import extract_user_turns

VERSION = 'screening-v5-tool-required'
ALLOWED_QUALITY_EXCEPTIONS = {'missing_tool_result', 'no_final_assistant_turn'}
FRAMEWORK = re.compile(
    r'^\s*<(teammate-message|agent-message|task-notification|subagent_notification|'
    r'local-command-stdout|local-command-caveat)\b[^>]*>[\s\S]*?</\1>\s*$', re.I)
COMPACTION = (
    'Another language model started to solve this problem and produced a summary',
    'You are performing a CONTEXT CHECKPOINT COMPACTION.',
    'This session is being continued from a previous conversation',
    'You are resuming a prior conversation. Its earlier turns were archived',
    'Your task is to create a detailed summary of the conversation so far, paying close attention',
)
SUGGESTION = '[SUGGESTION MODE: Suggest what the user might naturally type next into Claude Code.]'
COMMAND_BLOCK = re.compile(r'<(command-name|command-message|command-args|local-command-stdout|local-command-caveat)\b[^>]*>(.*?)</\1>\s*', re.S)
LOCAL_COMMANDS = {'model', 'clear', 'context', 'cost', 'help', 'login', 'logout', 'resume', 'compact', 'stats', 'status', 'usage'}
INTERRUPTED = re.compile(r'^\s*\[Request interrupted by user(?: for tool use)?\]\s*', re.I)
IDE_BLOCK = re.compile(r'<(ide_opened_file|ide_selection|bash-input|bash-stdout|bash-stderr)\b[^>]*>.*?</\1>\s*', re.S)
AUXILIARY_PATTERNS = (
    r'you are naming a coding session\b',
    r'(?:generate|write|produce|provide)[^.\n]{0,100}(?:title|session name)[^.\n]{0,80}(?:session|conversation|topic)',
    r'(?:generate|write|produce|provide)\s+(?:a\s+)?(?:concise|short)[^.\n]{0,70}title',
    r'you are[^.\n]{0,100}(?:summari[sz]er|notification (?:classifier|assistant)|security (?:reviewer|monitor|classifier))',
    r'you are performing a context checkpoint compaction',
    r'assess whether shell commands are safe',
    r'tasked with summarizing conversations',
    r'(?:summarize|summarise|produce a structured summary of) (?:the |this )?(?:conversation|transcript)',
    r'you are[^.\n]{0,80}(?:memory consolidation|memory extraction|rollout summarization)',
    r'(?:review|assess|classify)[^.\n]{0,90}(?:permission|approval) request',
)
SUBAGENT_PATTERNS = (
    r'(?:^|\n)\s*you are (?:an? )?(?:[^.\n]{0,70}\s)?sub[- ]?agent\b',
    r'(?:^|\n)\s*you are an agent for claude code\b',
    r'(?:^|\n)\s*you are a (?:file search specialist|software architect|planning agent|code reviewer) for claude code',
    r'(?:^|\n)\s*you (?:have been|were) (?:spawned|delegated)[^.\n]{0,90}(?:agent|task)',
    r'<multi_agent_role>\s*you are [`\'"]?/root/[^\s`\'"<]+',
)
TRIVIAL = re.compile(r'^(?:继续|好的?|对的?|是的?|执行|开始|确认|可以|谢谢|你好|ok|yes|go ahead|continue|thanks)[。.!！\s]*$', re.I)
MISSING_START = re.compile(r'^(?:implement the plan\.?|continue\.?|继续[。！]?|按照计划(?:执行|实施)[。！]?)$', re.I)


def text_of(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(text_of(item) for item in value)
    if isinstance(value, dict) and value.get('type') in ('text', 'input_text', 'output_text', None):
        return value.get('text', '') if isinstance(value.get('text', ''), str) else ''
    return ''


def instruction_text(record):
    fields = [record.get('instructions', '')]
    fields.extend(m.get('content') for m in record.get('messages', [])
                  if isinstance(m, dict) and m.get('role') in ('system', 'developer'))
    return '\n'.join(text_of(value) for value in fields)


def nodes(record):
    yield record
    for child in (record.get('sub_agent_trajectory') or {}).values():
        yield from nodes(child)


def observed_tool_calls(record):
    """Count explicit assistant tool invocations in the root transcript, never definitions or quoted text."""
    count = 0
    for message in record.get('messages', []):
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        calls = message.get('tool_calls')
        if isinstance(calls, list):
            for call in calls:
                function = call.get('function') if isinstance(call, dict) else None
                if isinstance(function, dict) and isinstance(function.get('name'), str) and function['name'].strip():
                    count += 1
        content = message.get('content')
        if isinstance(content, list):
            count += sum(isinstance(block, dict) and block.get('type') in ('tool_use', 'server_tool_use')
                         and isinstance(block.get('name'), str) and bool(block['name'].strip()) for block in content)
    return count


def caller_user_text(text, audit):
    """Unwrap only a boundary-aligned CLI exporter envelope, retaining actual command tasks."""
    text = text.strip()
    if text.startswith(SUGGESTION):
        audit['excluded_suggestion_mode'] = audit.get('excluded_suggestion_mode', 0) + 1
        return ''
    if not text.startswith(('<command-name>', '<command-message>')):
        return text
    command, queries = '', []
    while (match := COMMAND_BLOCK.match(text)):
        tag, body = match.groups()
        if tag == 'command-name':
            command = body.strip().lstrip('/')
        elif tag == 'command-message' and not command:
            command = body.strip().lstrip('/')
        elif tag == 'command-args' and body.strip() and command not in LOCAL_COMMANDS:
            queries.append(body.strip() if command == 'goal' else '/' + command + ' ' + body.strip())
        audit['removed_cli_exporter_blocks'] = audit.get('removed_cli_exporter_blocks', 0) + 1
        text = text[match.end():].lstrip()
    if text:
        queries.append(text)
    return '\n'.join(queries).strip()


def caller_content(content, audit):
    """Normalize explicit caller framing before the pinned user extractor sees it."""
    if isinstance(content, list):
        return [caller_content(block, audit) for block in content]
    if isinstance(content, dict):
        if content.get('type', '') in ('text', 'input_text', '') and isinstance(content.get('text'), str):
            return {**content, 'text': caller_content(content['text'], audit)}
        return content
    if not isinstance(content, str):
        return content
    text = content.lstrip()
    while (interrupted := INTERRUPTED.match(text)):
        audit['removed_interrupt_markers'] += 1
        text = text[interrupted.end():].lstrip()
    for prefix, reason in (
            ('[SYSTEM NOTIFICATION - NOT USER INPUT]', 'excluded_system_notification'),
            ('Base directory for this skill:', 'excluded_skill_expansion'),
            ('[Your previous response had no visible output. Please continue and produce a user-visible response.]',
             'excluded_automatic_retry_prompt')):
        if text.startswith(prefix):
            audit[reason] += 1
            return ''
    while (block := IDE_BLOCK.match(text)):
        audit['removed_ide_or_local_bash_blocks'] += 1
        text = text[block.end():].lstrip()
    return text


def assess(record):
    reasons, evidence = set(), []
    meta = record.get('metadata', {})
    comp = record.get('completeness') or {}
    instructions = instruction_text(record)
    if comp.get('is_subagent') or record.get('completeness_tag') == 'orphan_sub':
        reasons.add('subagent')
        evidence.append('completeness.is_subagent_or_orphan_sub')
    for key in ('parent_thread_id', 'parent_agent_id', 'parent_turn_id', 'subagent_marker'):
        if meta.get(key):
            reasons.add('subagent')
            evidence.append('metadata.' + key)
    for pattern in SUBAGENT_PATTERNS:
        if re.search(pattern, instructions, re.I):
            reasons.add('subagent')
            evidence.append('direct_subagent_instruction')
            break
    for pattern in AUXILIARY_PATTERNS:
        if re.search(pattern, instructions, re.I):
            reasons.add('auxiliary_model_call')
            evidence.append('caller_instruction_auxiliary_task')
            break
    for message in record.get('messages', []):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        text = text_of(message.get('content')).lstrip()
        if text.startswith('<codex_delegation>'):
            reasons.add('subagent')
            evidence.append('codex_delegation_envelope')
        if text.startswith(COMPACTION):
            reasons.add('compaction_history_incomplete')
            evidence.append('direct_continuation_or_archived_summary')
    for node in nodes(record):
        if node.get('compaction_items'):
            reasons.add('compaction_history_uncertain')
            evidence.append('compaction_items_present')
        metadata = node.get('metadata', {})
        if metadata.get('is_compaction') or metadata.get('epoch_cause') == 'compaction' or metadata.get('compaction'):
            reasons.add('compaction_history_incomplete')
            evidence.append('explicit_compaction_metadata')
    caller_audit = Counter()
    query_record = {**record, 'messages': [
        {**message, 'content': caller_content(message.get('content'), caller_audit)}
        if isinstance(message, dict) and message.get('role') == 'user' else message
        for message in record.get('messages', [])]}
    raw_turns, extraction = extract_user_turns(query_record)
    extraction.update(caller_audit)
    if extraction.get('unwrapped_delegation_input'):
        reasons.add('subagent')
        evidence.append('delegation_after_framework_removal')
    if extraction.get('removed_title_generation_instructions'):
        reasons.add('auxiliary_model_call')
        evidence.append('title_generation_user_envelope')
    clean, excluded_communication = [], []
    for turn in raw_turns:
        if FRAMEWORK.fullmatch(turn['text']):
            excluded_communication.append(turn['message_index'])
            continue
        turn = {**turn, 'text': caller_user_text(turn['text'], extraction)}
        if turn['text'].lstrip().startswith(COMPACTION):
            reasons.add('compaction_history_incomplete')
            evidence.append('continuation_after_caller_framing_removal')
            continue
        if turn['text'].startswith('<send_user_message_question_reply>'):
            match = re.search(r'<send_user_message_question_reply>(.*?)</send_user_message_question_reply>', turn['text'], re.S)
            try:
                replies = json.loads(match.group(1))
                answer = '\n'.join(item['answer'] for item in replies if isinstance(item.get('answer'), str))
                turn = {**turn, 'text': answer}
            except (ValueError, TypeError, AttributeError, KeyError):
                reasons.add('unparsed_user_reply_envelope')
                continue
        if turn['text'].strip():
            clean.append({**turn, 'turn_id': 'u' + str(len(clean) + 1).zfill(4)})
    extraction['excluded_agent_communication_messages'] = len(set(excluded_communication))
    if extraction.get('archived_history_may_be_incomplete') or extraction.get('excluded_compaction_summary'):
        reasons.add('compaction_history_incomplete')
    for key in ('unrecognized_content_blocks', 'unparsed_ide_wrapper', 'unparsed_agents_wrapper',
                'unparsed_response_annotations', 'unparsed_delegation', 'unparsed_open_design_wrapper',
                'invalid_messages', 'invalid_message', 'media_omitted'):
        if extraction.get(key):
            reasons.add('user_extraction_uncertain')
    if not clean:
        reasons.add('no_real_user_turn')
    elif MISSING_START.fullmatch(clean[0]['text'].strip()):
        reasons.add('first_user_requires_missing_context')
    if not any(len(turn['text'].strip()) >= 8 and not TRIVIAL.fullmatch(turn['text'].strip()) for turn in clean):
        reasons.add('no_substantive_user_request')
    if sum(len(turn['text']) for turn in clean) > 100000:
        reasons.add('user_text_exceeds_full_input_budget')
    basetraj = record.get('_basetraj', {})
    if basetraj.get('runtime_os') != 'linux':
        reasons.add('not_linux')
    audit = record.get('normalization_audit') or {}
    codes = set(audit.get('reason_codes') or [])
    if codes - ALLOWED_QUALITY_EXCEPTIONS:
        reasons.add('normalization_quality_errors')
    if (record.get('tool_defs_tag') != 'complete' or record.get('tool_call_tag') != 'consistent'
            or (record.get('tool_call_check') or {}).get('hallucinated_calls') != 0):
        reasons.add('invalid_tool_contract')
    if not isinstance(comp.get('is_subagent'), bool):
        reasons.add('missing_root_identity_assessment')
    if not basetraj.get('source_key'):
        reasons.add('missing_source_identity')
    observed_calls = observed_tool_calls(record)
    declared_calls = record.get('total_tool_calls')
    if type(declared_calls) is not int or declared_calls < 1 or observed_calls < 1:
        reasons.add('no_actual_tool_calls')
    real_rounds = len({turn['message_index'] for turn in clean})
    extraction['kept_user_turns'] = len(clean)
    first_query = clean[0]['text'] if clean else ''
    sid = meta.get('session_id')
    if sid and sid != 'no_session_id':
        group = [meta.get('specific_source'), meta.get('user_id'), sid]
    else:
        group = [meta.get('specific_source'), meta.get('user_id'), digest(first_query.encode())]
    return {
        'eligible': not reasons, 'rejection_reasons': sorted(reasons),
        'identity_evidence': sorted(set(evidence)), 'user_turns': clean,
        'real_user_rounds': real_rounds, 'user_segments': len(clean),
        'raw_user_messages': extraction.get('raw_user_messages', 0),
        'user_query_characters': sum(len(t['text']) for t in clean),
        'extraction_audit': extraction, 'quality_reason_codes': sorted(codes),
        'is_subagent': 'subagent' in reasons,
        'compaction_status': 'present_or_uncertain' if any(x.startswith('compaction_') for x in reasons) else 'no_observed_boundary',
        'root_complete': record.get('completeness_tag') in ('complete_main_no_sub', 'complete_main_with_complete_sub'),
        'session_group': digest(dumps(group).encode()),
        'user_query_sha256': digest(dumps([t['text'] for t in clean]).encode()),
        'screening_version': VERSION,
        'observed_tool_calls': observed_calls,
    }
