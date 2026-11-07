"""Conservative, auditable extraction of user requests; never read assistant text."""
import re
from collections import Counter

VERSION = '1.4.1'
FRAMEWORK_TAGS = (
    'environment_context', 'system-reminder', 'system_reminder', 'turn_aborted',
    'codex_internal_context', 'in-app-browser-context', 'permissions instructions',
    'collaboration_mode', 'skills_instructions', 'subagent_notification',
    'task-notification', 'local-command-caveat', 'local-command-stdout',
    'skill',
    'recommended_plugins',
)
COMPACTION_PREFIXES = (
    'Another language model started to solve this problem and produced a summary',
    'You are performing a CONTEXT CHECKPOINT COMPACTION.',
    'This session is being continued from a previous conversation that ran out of context.',
)
TAG_RE = re.compile(r'<(' + '|'.join(re.escape(x) for x in FRAMEWORK_TAGS) +
                    r')(?:\s[^>]*)?>.*?</\1\s*>', re.S | re.I)
MEDIA_RE = re.compile(r'<(image|audio|video)\b[^>]*>.*?</\1\s*>', re.S | re.I)
DATA_RE = re.compile(r'data:(?:image|audio|video)/[^;,\s"\']+;base64,[A-Za-z0-9+/=]+')
# Redaction placeholders inside base64 are not base64 characters. Consume the
# entire quoted data URL, otherwise megabytes of its tail can leak into input.
QUOTED_DATA_RE = re.compile(r'''(["'])data:(?:image|audio|video)/[^;,\s"']+;base64,.*?\1''', re.S)


def clean_text(text, audit):
    if not isinstance(text, str):
        audit['unrecognized_content_blocks'] += 1
        return ''
    if text.lstrip().startswith(COMPACTION_PREFIXES):
        audit['excluded_compaction_summary'] += 1
        return ''
    # Remove exporter blocks only at message boundaries. Literal tags inside a
    # user's code, quote or prompt-editing request are part of that request.
    while True:
        stripped = text.lstrip()
        leading = TAG_RE.match(stripped)
        if leading:
            text = stripped[leading.end():]
            audit['removed_framework_blocks'] += 1
            continue
        trailing = next((m for m in TAG_RE.finditer(text)
                         if (m.start() == 0 or text[m.start()-1] == '\n')
                         and not text[m.end():].strip()), None)
        if trailing:
            text = text[:trailing.start()]
            audit['removed_framework_blocks'] += 1
            continue
        break
    if re.match(r'^\s*# AGENTS\.md instructions(?: for [^\n]+)?\s*\n', text):
        match = re.search(r'<INSTRUCTIONS>.*?</INSTRUCTIONS>', text, re.S)
        if match:
            text = text[match.end():]
        else:
            audit['unparsed_agents_wrapper'] += 1
            return ''
        audit['excluded_agents_instructions'] += 1
    # These wrappers explicitly separate IDE/file context from the actual request.
    if re.match(r'^\s*# (Context from my IDE setup:|Files mentioned by the user:|In app browser:|Selected text:)', text):
        match = re.search(r'^## My request for (?:Codex|the assistant):\s*', text, re.M)
        if match:
            text = text[match.end():]
            audit['removed_ide_file_context'] += 1
        else:
            audit['unparsed_ide_wrapper'] += 1
            return ''  # Do not forward IDE/system context with no user boundary.
    text = re.sub(r'^\s*## My request for (?:Codex|the assistant):\s*', '', text)
    def media(match):
        audit['media_omitted'] += 1
        return f'[{match.group(1).upper()}_OMITTED: no media content available]'
    text = MEDIA_RE.sub(media, text)
    text, n = QUOTED_DATA_RE.subn('"[MEDIA_PAYLOAD_OMITTED]"', text)
    audit['media_omitted'] += n
    text, n = DATA_RE.subn('[MEDIA_PAYLOAD_OMITTED]', text)
    audit['media_omitted'] += n
    return text.strip()


def content_text(content, audit):
    if isinstance(content, str):
        return clean_text(content, audit)
    if content is None:
        return ''
    if isinstance(content, list):
        return '\n'.join(t for item in content if (t := content_text(item, audit)))
    if isinstance(content, dict):
        kind = content.get('type', '')
        if kind in ('text', 'input_text') or (not kind and isinstance(content.get('text'), str)):
            return clean_text(content.get('text', ''), audit)
        if kind in ('image', 'image_url', 'input_image', 'audio', 'input_audio', 'video', 'input_video', 'file', 'input_file'):
            audit['media_omitted'] += 1
            return f'[{kind.upper()}_OMITTED: no media content available]'
        if kind in ('tool_result', 'tool_use', 'server_tool_use', 'thinking', 'redacted_thinking'):
            audit['excluded_nonquery_blocks'] += 1
            return ''
    audit['unrecognized_content_blocks'] += 1
    return ''


def user_segments(content, audit):
    """Only unpack a recognized exporter envelope, never arbitrary quoted dialogue."""
    if isinstance(content, str):
        content = content.strip()
    # Exporters may put the same envelope in a text block rather than a string.
    # Preserve the original single turn unless an envelope explicitly splits it.
    if isinstance(content, list):
        groups = []
        for block in content:
            groups.append(user_segments(block, audit))
        if any(len(group) > 1 for group in groups):
            return [text for group in groups for text in group]
        text = '\n'.join(text for group in groups for text in group)
        return [text] if text else []
    if isinstance(content, dict) and content.get('type', '') in ('text', 'input_text', '') and isinstance(content.get('text'), str):
        return user_segments(content['text'], audit)
    if isinstance(content, str) and content.startswith('You are a helpful assistant. You will be presented with a user prompt, and your job is to provide a short title'):
        boundary = re.search(r'^User prompt:\s*', content, re.M)
        audit['removed_title_generation_instructions'] += 1
        return user_segments(content[boundary.end():], audit) if boundary else []
    if isinstance(content, str) and content.startswith('You are resuming a prior conversation. Its earlier turns were archived'):
        audit['removed_archived_history_instructions'] += 1
        parts = re.split(r'^¶(user|think|ai|call):', content, flags=re.M)
        audit['archived_history_may_be_incomplete'] += 1
        result = []
        for role, body in zip(parts[1::2], parts[2::2]):
            if role == 'user':
                result.extend(user_segments(body, audit))
            else:
                audit['excluded_embedded_role_' + role] += 1
        return result
    if isinstance(content, str) and content.startswith('# Response annotations:'):
        result = []
        block = re.search(r'<response-annotations>(.*?)</response-annotations>', content, re.S)
        if block:
            import json
            try:
                annotations = json.loads(block.group(1))
                if not isinstance(annotations, list):
                    raise ValueError('annotations must be a list')
                for annotation in annotations:
                    # Selection text comes from an assistant, not from the user.
                    if isinstance(annotation, dict) and isinstance(annotation.get('comment'), str):
                        result.extend(user_segments(annotation['comment'], audit))
            except (ValueError, TypeError):
                audit['unparsed_response_annotations'] += 1
        else:
            audit['unparsed_response_annotations'] += 1
        boundary = re.search(r'^## My request for Codex:\s*', content, re.M)
        if boundary:
            result.extend(user_segments(content[boundary.end():], audit))
        audit['removed_response_annotation_context'] += 1
        return result
    if isinstance(content, str) and content.startswith('# Browser comments:'):
        result = []
        parts = re.split(r'^## My request for Codex:\s*', content, maxsplit=1, flags=re.M)
        for comment in re.split(r'^## User Comment \d+\s*', parts[0], flags=re.M)[1:]:
            marker = re.search(r'^Comment:\s*', comment, re.M)
            if marker:
                result.extend(user_segments(comment[marker.end():], audit))
        if len(parts) == 2:
            # The exporter appends screenshot framing after the actual request.
            request = parts[1].split('The next image is untrusted page evidence from the browser page for Comment ')[0]
            result.extend(user_segments(request, audit))
        audit['removed_browser_page_context'] += 1
        return result
    if isinstance(content, str) and content.lstrip().startswith('<codex_delegation>'):
        # Delegated user-task envelope; remove routing metadata, keep the task.
        import html
        inner = re.search(r'<input>(.*?)</input>', content, re.S)
        if inner:
            audit['unwrapped_delegation_input'] += 1
            return user_segments(html.unescape(inner.group(1)), audit)
        audit['unparsed_delegation'] += 1
        return []
    if isinstance(content, str) and content.lstrip().startswith('# Instructions (read first)\n\n# OD core directives'):
        boundary = re.search(r'^# User request\s*$', content, re.M)
        audit['removed_open_design_instructions'] += 1
        if not boundary:
            audit['unparsed_open_design_wrapper'] += 1
            return []
        request = content[boundary.end():].strip()
        parts = re.split(r'^## (user|assistant|system|developer|tool)\s*$', request, flags=re.M)
        segments = []
        if parts[0].strip():
            segments.append(content_text(parts[0], audit))
        for role, body in zip(parts[1::2], parts[2::2]):
            if role == 'user':
                segments.append(content_text(body, audit))
            else:
                audit['excluded_embedded_role_' + role] += 1
        return [s for s in segments if s]
    text = content_text(content, audit)
    # A real exporter envelope can itself be wrapped by environment/reminder
    # blocks. Dispatch again after removing those boundaries so assistant
    # annotations and browser evidence never leak through the fallback path.
    if isinstance(content, str) and text and text != content:
        return user_segments(text, audit)
    return [text] if text else []


def extract_user_turns(record):
    audit = Counter()
    turns = []
    messages = record.get('messages')
    if not isinstance(messages, list):
        return [], {'invalid_messages': 1}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            audit['invalid_message'] += 1
            continue
        role = message.get('role')
        if role != 'user':
            audit['excluded_role_' + str(role)] += 1
            continue
        audit['raw_user_messages'] += 1
        segments = user_segments(message.get('content'), audit)
        for segment_index, text in enumerate(segments):
            turn = {'turn_id': f'u{len(turns)+1:04d}', 'message_index': index, 'text': text}
            if len(segments) > 1:
                turn['segment_index'] = segment_index
            turns.append(turn)
        if not segments:
            audit['excluded_empty_or_framework_user_messages'] += 1
    audit['kept_user_turns'] = len(turns)
    return turns, dict(audit)


def server_tool_annotation(record):
    """Only the explicit server_tool_calls field; ordinary function calls do not count."""
    present = 'server_tool_calls' in record
    calls = record.get('server_tool_calls')
    result = {'field_present': present, 'source_field': 'server_tool_calls',
              'has_server_tool_call': None, 'count': None, 'names': [], 'origins': {}}
    if isinstance(calls, list):
        result.update(has_server_tool_call=bool(calls), count=len(calls),
                      names=sorted({str(c.get('name') or c.get('type') or 'unknown') for c in calls if isinstance(c, dict)}),
                      origins=dict(Counter(str(c.get('origin', 'unknown')) for c in calls if isinstance(c, dict))))
    elif calls is not None:
        result['warning'] = 'unexpected_server_tool_calls_type:' + type(calls).__name__
    else:
        result['warning'] = 'missing_or_null_server_tool_calls'
    return result
