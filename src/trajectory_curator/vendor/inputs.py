"""Input loading and payload building: only real user turns reach the model, compressed and recorded."""
from pathlib import Path
import re

from .storage import dumps, strict_json

# Same allowlist as the sibling 分类 project: extraction notes the model may see (only when truthy).
EXTRACTION_NOTE_KEYS = (
    'media_omitted', 'unrecognized_content_blocks', 'unparsed_ide_wrapper', 'invalid_messages',
    'archived_history_may_be_incomplete', 'unparsed_response_annotations', 'unparsed_delegation',
    'unparsed_open_design_wrapper', 'unparsed_agents_wrapper', 'invalid_message')

DEFAULT_TURN_CAP = 6000
DEFAULT_TOTAL_BUDGET = 120000
MIN_TURN_CAP = 200
HEAD_FRACTION = 0.75  # 4500 of 6000
CAP_SHRINK = 0.85

# [lo, hi) context-token buckets of the sample200 pool
BUCKET_EDGES = ((0, 100_000, '0-100k'), (100_000, 150_000, '100-150k'),
                (150_000, 400_000, '150-400k'), (400_000, None, '400k+'))

_ID_RE = re.compile(r'^\{\s*"trajectory_id"\s*:\s*"([^"]+)"')


def bucket_of(tokens):
    if not isinstance(tokens, (int, float)) or isinstance(tokens, bool) or tokens < 0:
        return None
    for lo, hi, name in BUCKET_EDGES:
        if tokens >= lo and (hi is None or tokens < hi):
            return name
    return None


def normalize_item(raw):
    """Check an input record (real trajectory line or synthetic case) and return a uniform dict."""
    if not isinstance(raw, dict) or not isinstance(raw.get('trajectory_id'), str) or not raw['trajectory_id']:
        raise ValueError('input record needs a string trajectory_id')
    tid = raw['trajectory_id']
    turns = raw.get('user_turns')
    if not isinstance(turns, list):
        raise ValueError(f'{tid}: user_turns must be a list')
    seen = set()
    clean = []
    for t in turns:
        if not isinstance(t, dict) or not isinstance(t.get('turn_id'), str) or not isinstance(t.get('text'), str):
            raise ValueError(f'{tid}: every user turn needs string turn_id and text')
        if t['turn_id'] in seen:
            raise ValueError(f'{tid}: duplicate turn_id {t["turn_id"]}')
        seen.add(t['turn_id'])
        turn = {'turn_id': t['turn_id'], 'text': t['text']}
        if 'segment_index' in t:
            turn['segment_index'] = t['segment_index']
        clean.append(turn)
    audit = raw.get('extraction_audit')
    item = {'trajectory_id': tid, 'user_turns': clean,
            'extraction_audit': audit if isinstance(audit, dict) else {}}
    source = raw.get('source')
    ix = source.get('sxf_index') if isinstance(source, dict) else None
    if isinstance(ix, dict):
        item['context_tokens_estimate'] = ix.get('context_tokens_estimate')
        item['harness'] = ix.get('harness')
    if 'user_query_characters' in raw:
        item['user_query_characters'] = raw['user_query_characters']
    if 'expected' in raw:
        item['expected'] = raw['expected']  # never sent, never used by the runner
    return item


def read_ids(path):
    ids = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            ids.append(line)
    if len(set(ids)) != len(ids):
        raise ValueError(f'{path}: duplicate ids')
    return ids


def load_items_by_ids(path, ids):
    """One streaming pass over the big user_queries.jsonl; returns normalized items in `ids` order."""
    wanted = set(ids)
    found = {}
    if not wanted:
        return []
    with open(path, encoding='utf-8') as f:
        for line in f:
            m = _ID_RE.match(line)
            if m and m.group(1) not in wanted:
                continue
            raw = strict_json(line)
            if raw.get('trajectory_id') in wanted:
                found[raw['trajectory_id']] = normalize_item(raw)
                if len(found) == len(wanted):
                    break
    missing = [i for i in ids if i not in found]
    if missing:
        raise ValueError(f'{len(missing)} ids not found in {path}, e.g. {missing[:3]}')
    return [found[i] for i in ids]


def load_cases(path):
    items = []
    for n, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            items.append(normalize_item(strict_json(line)))
        except ValueError as exc:
            raise ValueError(f'{path}:{n}: {exc}') from None
    ids = [i['trajectory_id'] for i in items]
    if len(set(ids)) != len(ids):
        raise ValueError(f'{path}: duplicate trajectory_id')
    return items


def item_meta(item):
    """Provenance-only fields attached to the result; never sent to the model."""
    meta = {}
    if item.get('context_tokens_estimate') is not None:
        meta['context_tokens_estimate'] = item['context_tokens_estimate']
        bucket = bucket_of(item['context_tokens_estimate'])
        if bucket:
            meta['bucket'] = bucket
    if item.get('harness') is not None:
        meta['harness'] = item['harness']
    return meta


# ---- compression -------------------------------------------------------------------------------

def collapse_duplicates(turns):
    """Merge consecutive turns with exactly equal text into the first one. Returns (turns, groups)."""
    out, groups = [], []
    for t in turns:
        if out and out[-1]['text'] == t['text']:
            out[-1]['_ids'].append(t['turn_id'])
        else:
            out.append({**t, '_ids': [t['turn_id']]})
    for t in out:
        if len(t['_ids']) > 1:
            groups.append({'first': t['_ids'][0], 'last': t['_ids'][-1], 'count': len(t['_ids']),
                           'turn_ids': list(t['_ids'])})
    return out, groups


def truncate_text(text, cap):
    """Keep head (75%) + tail (25%) of `cap` chars with a marker; returns (text, removed_chars)."""
    if len(text) <= cap:
        return text, 0
    head = int(cap * HEAD_FRACTION)
    tail = cap - head
    removed = len(text) - head - tail
    marker = f'…[已截断 {removed} 字]…'
    return text[:head] + marker + (text[-tail:] if tail else ''), removed


def _render(turns, cap):
    rendered, truncated = [], []
    for t in turns:
        text, removed = truncate_text(t['text'], cap)
        if removed:
            truncated.append({'turn_id': t['turn_id'], 'original_chars': len(t['text']), 'removed_chars': removed})
        if len(t['_ids']) > 1:
            note = f'[同一内容连续重复 {len(t["_ids"])} 次: {t["_ids"][0]}–{t["_ids"][-1]}]'
            text = text + '\n' + note if text else note
        entry = {'turn_id': t['turn_id']}
        if 'segment_index' in t:
            entry['segment_index'] = t['segment_index']
        entry['text'] = text
        rendered.append(entry)
    return rendered, truncated


def build_payload(item, turn_cap=DEFAULT_TURN_CAP, total_budget=DEFAULT_TOTAL_BUDGET):
    """Return (payload, input_stats). Never drops a whole turn (duplicates are merged, not dropped)."""
    original = item['user_turns']
    collapsed, groups = collapse_duplicates(original)
    cap = turn_cap
    while True:
        rendered, truncated = _render(collapsed, cap)
        total = sum(len(t['text']) for t in rendered)
        if not total_budget or total <= total_budget or cap <= MIN_TURN_CAP:
            break
        cap = max(MIN_TURN_CAP, int(cap * CAP_SHRINK))
    notes = {k: v for k, v in item.get('extraction_audit', {}).items() if v and k in EXTRACTION_NOTE_KEYS}
    payload = {'user_turns': rendered, 'extraction_notes': notes}
    stats = {
        'n_turns_original': len(original),
        'n_turns_sent': len(rendered),
        'chars_original': sum(len(t['text']) for t in original),
        'chars_sent': total,
        'duplicate_groups': [{k: g[k] for k in ('first', 'last', 'count')} for g in groups],
        'duplicate_turns_merged': sum(g['count'] - 1 for g in groups),
        'truncated_turns': truncated,
        'turn_cap_configured': turn_cap,
        'turn_cap_effective': cap,
        'total_budget': total_budget,
        'budget_exceeded': bool(total_budget and total > total_budget),
    }
    stats['payload_chars'] = len(dumps(payload))
    return payload, stats


def payload_json(payload):
    return dumps(payload)
