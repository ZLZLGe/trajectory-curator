"""Check demand references against user turns without inferring missing demands."""
import re

from .vendor.inputs import collapse_duplicates

POLICY_VERSION = 'user-turn-coverage-v1'

# Only complete, known non-demand replies are optional. Short requests still count.
OPTIONAL_REPLY = re.compile(
    r'(?:好|好的|好吧|对|对的|是|是的|收到|明白|确认|可以|继续|执行|开始|开干|'
    r'谢谢|你好|进度如何|怎么样了|到哪一步了|'
    r'ok|okay|yes|go ahead|continue|thanks|thank you|[a-dａ-ｄ])', re.I)


def assess_coverage(user_turns, model_output):
    """Report unreferenced turns; consecutive duplicates share reference coverage."""
    referenced = {turn_id for demand in model_output['demands']
                  for turn_id in demand['source_turns']}
    collapsed, _ = collapse_duplicates(user_turns)
    covered = set()
    for turn in collapsed:
        if referenced.intersection(turn['_ids']):
            covered.update(turn['_ids'])
    required, exempt = [], []
    for turn in user_turns:
        text = turn['text'].strip().rstrip('。.!！?？~～').strip()
        target = exempt if not text or OPTIONAL_REPLY.fullmatch(text) else required
        target.append(turn['turn_id'])
    return {
        'policy_version': POLICY_VERSION,
        'required_turn_ids': required,
        'covered_turn_ids': [turn['turn_id'] for turn in user_turns
                             if turn['turn_id'] in covered],
        'exempt_turn_ids': exempt,
        'uncovered_turn_ids': [turn_id for turn_id in required if turn_id not in covered],
    }


def with_coverage_review(record, user_turns):
    """Keep valid model output, but require review when user turns are unaccounted for."""
    audit = assess_coverage(user_turns, record['model_output'])
    output = dict(record['model_output'])
    if audit['uncovered_turn_ids']:
        reason = 'uncovered_user_turns: ' + ', '.join(audit['uncovered_turn_ids'])
        previous = output['review_reason']
        output['needs_review'] = True
        if reason not in previous:
            output['review_reason'] = previous + ('; ' if previous else '') + reason
    return {**record, 'model_output': output, 'coverage_audit': audit}
