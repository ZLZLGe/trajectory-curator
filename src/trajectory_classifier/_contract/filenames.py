"""Stable normalized trajectory filenames. Extracted from TrajFoundry export."""

from __future__ import annotations

import re

from .canonical import trajectory_id
from .models import TrajectoryNode

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SYNTHESIZED_SESSION_CODE = "metadata_session_id_synthesized"


def _session_was_synthesized(node: TrajectoryNode) -> bool:
    if not node.metadata.session_id:
        return True
    audit = node.normalization_audit
    return bool(
        audit and any(issue.code == _SYNTHESIZED_SESSION_CODE for issue in audit.issues)
    )


def trajectory_filename(node: TrajectoryNode, identifier: str | None = None) -> str:
    """Return the stable root filename for one materialized trajectory."""

    sub_session_id = node.metadata.sub_session_id
    if type(sub_session_id) is not int or sub_session_id < 0:
        raise ValueError("sub_session_id must be a non-negative integer")
    if _session_was_synthesized(node):
        if sub_session_id != 0:
            raise ValueError("sessionless trajectories must use sub_session_id 0")
        digest = identifier or trajectory_id(node)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("trajectory identifier must be a sha256 digest")
        return f"no_session_id_{digest}_sub_0.jsonl"

    session_id = node.metadata.session_id
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        raise ValueError(
            "session_id must contain only ASCII letters, digits, '.', '_' or '-' "
            "and must be at most 200 characters"
        )
    return f"{session_id}_sub_{sub_session_id}.jsonl"
