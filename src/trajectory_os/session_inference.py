"""Use unanimous sibling runtime evidence within one dated UUID session."""

import re
from collections import Counter, defaultdict

from .runtime_os import classify

SESSION = re.compile(
    r"/dt=([^/]+)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_sub_\d+\.jsonl$"
)


def session_key(source_key, harness):
    match = SESSION.search(source_key)
    return f"{harness}/{match[1]}/{match[2]}" if match else None


def build_session_map(records):
    """Build anchors from a complete iterable of BaseTraj session records.

    Each record must carry ``_basetraj.source_key`` and ``_basetraj.harness``.
    Read original runtime evidence, ignoring any stored OS annotation. The
    original metadata session ID must agree with the dated UUID source path.
    Only sessions with evidence-free records need an anchor in the result.
    """
    known = defaultdict(set)
    counts = Counter()
    anchors = {}
    conflicts = set()
    unknown_sessions = set()
    priorities = {"explicit": 0, "environment_path": 1, "local_tool": 2}
    for record in records:
        export = record["_basetraj"]
        key = session_key(export["source_key"], export["harness"])
        if key is None:
            continue
        if record.get("metadata", {}).get("session_id") != key.rsplit("/", 1)[-1]:
            conflicts.add(key)
            continue
        annotation = classify(record)
        method = annotation["runtime_os_method"]
        reason = annotation.get("runtime_os_reason", "")
        if annotation["runtime_os"] != "unknown" and method in priorities:
            known[key].add(annotation["runtime_os"])
            counts[key] += 1
            candidate = {
                "source_key": export["source_key"],
                "runtime_os": annotation["runtime_os"],
                "method": method,
            }
            previous = anchors.get(key)
            if previous is None or (priorities[method], candidate["source_key"]) < (
                priorities[previous["method"]],
                previous["source_key"],
            ):
                anchors[key] = candidate
        elif reason.startswith("conflicting_") or reason == "unsupported_os":
            conflicts.add(key)
        elif reason == "no_runtime_evidence":
            unknown_sessions.add(key)
    return {
        key: {**anchors[key], "supporting_records": counts[key]}
        for key in sorted(unknown_sessions & anchors.keys())
        if len(known[key]) == 1 and key not in conflicts
    }


def refine(record, annotation, mapping):
    if annotation.get("runtime_os_reason") != "no_runtime_evidence":
        return annotation
    export = record["_basetraj"]
    key = session_key(export["source_key"], export["harness"])
    anchor = mapping.get(key)
    if not anchor:
        return annotation
    # A filesystem name alone is insufficient: the original metadata must
    # independently identify the same UUID session.
    if record.get("metadata", {}).get("session_id") != key.rsplit("/", 1)[-1]:
        return annotation
    return {
        "runtime_os": anchor["runtime_os"],
        "runtime_os_method": "session_context",
        "runtime_os_evidence": [
            {
                "os": anchor["runtime_os"],
                "source": "same_session:" + anchor["source_key"],
                "value": f"{anchor['supporting_records']} non-conflicting sibling records; anchor method={anchor['method']}",
            }
        ],
    }
