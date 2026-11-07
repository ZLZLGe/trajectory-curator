"""Session/query deduplication and deterministic sampling across source strata."""
import math
from collections import Counter, defaultdict, deque
from pathlib import Path

from .common import atomic_json, digest, dumps, file_hash, read_jsonl


def deduplicate(items):
    """Collapse connected duplicate sessions OR identical full user-query sequences."""
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen = {}
    for i, item in enumerate(items):
        for field in ('session_group', 'user_query_sha256', 'trajectory_id'):
            key = field, item[field]
            if key in seen:
                parent[find(i)] = find(seen[key])
            seen[key] = i
    groups = defaultdict(list)
    for i, item in enumerate(items):
        groups[find(i)].append(item)

    def priority(item):
        return (item['root_complete'], not item['quality_reason_codes'], item['real_user_rounds'],
                item['total_rounds'], item['total_tool_calls'], item['context_tokens'], item['trajectory_id'])

    return [max(group, key=priority) for group in groups.values()]


def choose(items, count, seed='trajectory-curator-v1'):
    """Square-root allocation by harness/length; interleave models within each stratum."""
    if count < 1 or count > len(items):
        raise ValueError(f'need {count} unique candidates, have {len(items)}')
    groups = defaultdict(lambda: defaultdict(list))
    for item in items:
        groups[item['harness'], item['context_bucket']][item['model']].append(item)
    queues, weights = {}, {}
    for group, models in sorted(groups.items()):
        buckets = [deque(sorted(rows, key=lambda x: digest((seed + '/' + x['trajectory_id']).encode())))
                   for _, rows in sorted(models.items())]
        queue = deque()
        while any(buckets):
            for bucket in buckets:
                if bucket:
                    queue.append(bucket.popleft())
        queues[group] = queue
        weights[group] = math.sqrt(len(queue))
    used = Counter()
    selected = []
    while len(selected) < count:
        group = min((g for g, q in queues.items() if q), key=lambda g: ((used[g] + 1) / weights[g], g))
        selected.append(queues[group].popleft())
        used[group] += 1
    return selected


def select(candidates, output, count=1750, seed='trajectory-curator-v1'):
    all_items = list(read_jsonl(candidates))
    complete = [i for i in all_items if i['eligible'] and i['root_complete'] and not i['quality_reason_codes']]
    unique = deduplicate(complete)
    selected = choose(unique, count, seed)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w') as handle:
        for item in selected:
            handle.write(dumps(item) + '\n')
    summary = {'candidate_rows': len(all_items), 'complete_rows': len(complete), 'unique_complete': len(unique),
               'selected': len(selected), 'seed': seed, 'selection': 'sqrt_harness_context_model_interleave',
               'harness': dict(Counter(x['harness'] for x in selected)),
               'context_bucket': dict(Counter(x['context_bucket'] for x in selected)),
               'real_user_rounds': dict(Counter(x['real_user_rounds'] for x in selected)),
               'output_sha256': file_hash(output)}
    atomic_json(output.with_suffix('.summary.json'), summary)
    print(dumps(summary), flush=True)
    return selected
