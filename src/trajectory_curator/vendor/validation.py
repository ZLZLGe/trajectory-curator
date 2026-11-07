"""Strict validation of the model's JSON output. Error messages are short and are fed back to the model on retry.

Enums that come from configs/rubric.json (deliverables, time-bucket ids, dimension ranges, max_demands) are read
from the Rubric object; the purely structural enums below are not part of the rubric file.
"""
from .rubric import TIME_DIM

STATUSES = ('active', 'cancelled')
STRUCTURES = ('atomic', 'sequential', 'parallel', 'mixed')
RELATION_TYPES = ('none', 'independent', 'same_project', 'follows_up')
LINKING_RELATIONS = ('same_project', 'follows_up')

TOP_KEYS = ('demands', 'primary_demand', 'needs_review', 'review_reason')
DEMAND_KEYS = ('id', 'summary', 'source_turns', 'status', 'deliverable', 'structure', 'subtasks',
               'relation', 'difficulty')
RELATION_KEYS = ('type', 'to')

MAX_SUMMARY_CHARS = 400
MAX_RATIONALE_CHARS = 300
MAX_SUBTASKS = 8


def _is_int(x):
    return type(x) is int  # rejects bool


def _check_keys(obj, expected, where):
    if not isinstance(obj, dict):
        raise ValueError(f'{where} must be a JSON object')
    missing = [k for k in expected if k not in obj]
    extra = [k for k in obj if k not in expected]
    if missing or extra:
        raise ValueError(f'{where} keys must be exactly {list(expected)}; missing {missing}, unexpected {extra}')


def _choice(value, options, where):
    if not isinstance(value, str) or value not in options:
        raise ValueError(f'{where} must be one of {list(options)}')


def validate_output(obj, turn_ids, rubric):
    """Validate `obj` (the parsed model JSON). Returns obj; raises ValueError with an actionable message."""
    turn_ids = set(turn_ids)
    _check_keys(obj, TOP_KEYS, 'top-level object')
    needs_review, reason = obj['needs_review'], obj['review_reason']
    if not isinstance(needs_review, bool):
        raise ValueError('needs_review must be a boolean')
    if not isinstance(reason, str):
        raise ValueError('review_reason must be a string ("" when needs_review is false)')
    if needs_review and not reason.strip():
        raise ValueError('review_reason must be non-empty when needs_review is true')
    demands = obj['demands']
    if not isinstance(demands, list):
        raise ValueError('demands must be a list')
    if len(demands) > rubric.max_demands:
        raise ValueError(f'too many demands ({len(demands)}); at most {rubric.max_demands}, merge closely related ones')
    primary = obj['primary_demand']
    if not demands:
        if not needs_review:
            raise ValueError('empty demands is allowed only with needs_review=true and a review_reason')
        if primary is not None:
            raise ValueError('primary_demand must be null when demands is empty')
        return obj
    ids = []
    for index, demand in enumerate(demands):
        _validate_demand(demand, index, ids, turn_ids, rubric)
        ids.append(demand['id'])
    active = [d['id'] for d in demands if d['status'] == 'active']
    if active:
        if primary not in active:
            raise ValueError(f'primary_demand must be the id of an active demand, one of {active}')
    elif primary is not None and primary not in ids:
        raise ValueError(f'primary_demand must be null or one of {ids}')
    return obj


def _validate_demand(demand, index, earlier_ids, turn_ids, rubric):
    expected_id = f'D{index + 1}'
    where = expected_id
    _check_keys(demand, DEMAND_KEYS, where)
    if demand['id'] != expected_id:
        raise ValueError(f'demand #{index + 1} must have id "{expected_id}" (ids are D1..Dn in order)')
    summary = demand['summary']
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError(f'{where}.summary must be a non-empty string')
    if len(summary) > MAX_SUMMARY_CHARS:
        raise ValueError(f'{where}.summary must be at most {MAX_SUMMARY_CHARS} characters')
    turns = demand['source_turns']
    if not isinstance(turns, list) or not turns:
        raise ValueError(f'{where}.source_turns must be a non-empty list of turn ids')
    for t in turns:
        if not isinstance(t, str) or t not in turn_ids:
            raise ValueError(f'{where}.source_turns contains {t!r}, which is not a turn_id in the input')
    if len(set(turns)) != len(turns):
        raise ValueError(f'{where}.source_turns must not repeat a turn id')
    _choice(demand['status'], STATUSES, f'{where}.status')
    _choice(demand['deliverable'], rubric.deliverables, f'{where}.deliverable')
    _choice(demand['structure'], STRUCTURES, f'{where}.structure')
    subtasks = demand['subtasks']
    if (not isinstance(subtasks, list) or not 1 <= len(subtasks) <= MAX_SUBTASKS
            or not all(isinstance(s, str) and s.strip() for s in subtasks)):
        raise ValueError(f'{where}.subtasks must be a list of 1..{MAX_SUBTASKS} non-empty strings')
    if (demand['structure'] == 'atomic') != (len(subtasks) == 1):
        raise ValueError(f'{where}: structure must be "atomic" exactly when subtasks has one item')
    _validate_relation(demand['relation'], expected_id, earlier_ids, where)
    _validate_difficulty(demand['difficulty'], demand['status'], where, rubric)


def _validate_relation(relation, demand_id, earlier_ids, where):
    _check_keys(relation, RELATION_KEYS, f'{where}.relation')
    rtype, to = relation['type'], relation['to']
    _choice(rtype, RELATION_TYPES, f'{where}.relation.type')
    if demand_id == 'D1':
        if rtype != 'none' or to is not None:
            raise ValueError('D1.relation must be {"type":"none","to":null}')
        return
    if rtype == 'none':
        raise ValueError(f'{where}.relation.type must not be "none" for D2 and later; use independent, same_project or follows_up')
    if rtype == 'independent':
        if to is not None:
            raise ValueError(f'{where}.relation.to must be null when type is "independent"')
        return
    if not isinstance(to, str) or to not in earlier_ids:
        raise ValueError(f'{where}.relation.to must be an earlier demand id {earlier_ids} when type is "{rtype}"')


def _validate_difficulty(diff, status, where, rubric):
    if diff is None:
        if status != 'cancelled':
            raise ValueError(f'{where}.difficulty may be null only for cancelled demands')
        return
    w = f'{where}.difficulty'
    _check_keys(diff, rubric.difficulty_keys, w)
    for dim in rubric.dimensions:
        value = diff[dim]
        if dim == TIME_DIM:
            if not isinstance(value, str) or value not in rubric.time_ids:
                raise ValueError(f'{w}.{dim} must be one of {rubric.time_ids}')
        elif dim in rubric.count_dims:
            if not _is_int(value) or value < 0:
                raise ValueError(f'{w}.{dim} must be an integer >= 0')
        else:
            top = rubric.dimensions[dim]['max']
            if not _is_int(value) or not 0 <= value <= top:
                raise ValueError(f'{w}.{dim} must be an integer in 0..{top}')
    rationale = diff['rationale']
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError(f'{w}.rationale must be a non-empty string')
    if len(rationale) > MAX_RATIONALE_CHARS:
        raise ValueError(f'{w}.rationale must be at most {MAX_RATIONALE_CHARS} characters')
