"""Classify the root agent's runtime OS using local trajectory evidence."""

from .runtime_os import NEW_FIELDS, OS_VALUES, classify
from .session_inference import build_session_map, refine, session_key

__all__ = [
    "NEW_FIELDS",
    "OS_VALUES",
    "build_session_map",
    "classify",
    "refine",
    "session_key",
]
