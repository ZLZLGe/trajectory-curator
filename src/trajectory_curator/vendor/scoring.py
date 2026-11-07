"""Deterministic difficulty scoring and per-trajectory derived fields, driven entirely by the rubric."""
from collections import Counter

from .rubric import TIME_DIM
from .validation import LINKING_RELATIONS, RELATION_TYPES


def count_score(value, rows, dim):
    """rows are [lo, hi_inclusive_or_null, score]."""
    for lo, hi, score in rows:
        if value >= lo and (hi is None or value <= hi):
            return score
    raise ValueError(f'{dim}={value} falls in no count bin')


def sub_scores(difficulty, rubric):
    """Map each rubric dimension to its integer sub-score (0..max)."""
    out = {}
    for dim in rubric.dimensions:
        value = difficulty[dim]
        if dim == TIME_DIM:
            out[dim] = rubric.time_by_id[value]['score']
        elif dim in rubric.count_bins:
            out[dim] = count_score(value, rubric.count_bins[dim], dim)
        else:
            out[dim] = value
    return out


def weighted_score(subs, rubric):
    """100 * sum_d w_d * (s_d / max_d) / sum_d w_d, rounded to 1 decimal."""
    total_w = sum(spec['weight'] for spec in rubric.dimensions.values())
    acc = sum(spec['weight'] * (subs[dim] / spec['max']) for dim, spec in rubric.dimensions.items())
    return round(100 * acc / total_w, 1)


def level_of(score, rubric):
    for lo, hi, level in rubric.levels:
        if lo <= score < hi:
            return level
    raise ValueError(f'score {score} falls in no level range')


def score_difficulty(difficulty, rubric):
    subs = sub_scores(difficulty, rubric)
    score = weighted_score(subs, rubric)
    return {'sub_scores': subs, 'difficulty_score': score, 'level': level_of(score, rubric),
            'expert_time': difficulty[TIME_DIM],
            'expert_minutes': rubric.time_by_id[difficulty[TIME_DIM]]['minutes']}


def count_groups(demands):
    """Connected components of active demands joined by same_project / follows_up relations.

    An edge needs both endpoints active; a link to a cancelled demand does not connect anything.
    """
    active = [d['id'] for d in demands if d['status'] == 'active']
    parent = {i: i for i in active}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for d in demands:
        rel = d['relation']
        if d['status'] == 'active' and rel['type'] in LINKING_RELATIONS and rel['to'] in parent:
            parent[find(d['id'])] = find(rel['to'])
    return len({find(i) for i in active})


def derive(output, rubric):
    """Code-computed fields for one validated model output."""
    demands = output['demands']
    per_demand, scored = [], {}
    for d in demands:
        s = score_difficulty(d['difficulty'], rubric) if d['difficulty'] is not None else None
        scored[d['id']] = s
        per_demand.append({'id': d['id'], 'status': d['status'], 'scoring': s})
    active = [d for d in demands if d['status'] == 'active']
    cancelled = [d for d in demands if d['status'] == 'cancelled']
    relation_counts = Counter({t: 0 for t in RELATION_TYPES if t != 'none'})
    for d in active:
        if d['relation']['type'] != 'none':
            relation_counts[d['relation']['type']] += 1
    active_scored = [scored[d['id']] for d in active]
    primary_id = output['primary_demand']
    primary = scored.get(primary_id) if primary_id else None
    best = max(active_scored, key=lambda s: s['difficulty_score']) if active_scored else None
    return {
        'n_demands': len(active),
        'n_cancelled': len(cancelled),
        'is_multi_demand': len(active) >= 2,
        'n_groups': count_groups(demands),
        'relation_counts': dict(relation_counts),
        'relations': [d['relation']['type'] for d in demands[1:]],
        'primary_demand': primary_id,
        'primary_difficulty_score': primary['difficulty_score'] if primary else None,
        'primary_difficulty_level': primary['level'] if primary else None,
        'primary_expert_time': primary['expert_time'] if primary else None,
        'max_difficulty_score': best['difficulty_score'] if best else None,
        'max_difficulty_level': max(s['level'] for s in active_scored) if active_scored else None,
        'total_expert_minutes': sum(s['expert_minutes'] for s in active_scored),
        'demands': per_demand,
    }
