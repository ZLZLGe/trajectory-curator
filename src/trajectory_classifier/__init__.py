"""Standalone trajectory classification, with optional dependencies loaded on demand."""

from importlib import import_module

__version__ = "0.2.0"

_EXPORTS = {
    **dict.fromkeys(
        (
            "CLASSIFICATION_POLICY",
            "CLASSIFIER_REVISION",
            "DEFAULT_MAX_CONTEXT_CHARS",
            "DEFAULT_MAX_OUTPUT_TOKENS",
            "DEFAULT_SEMANTIC_RETRIES",
            "MAX_CONTEXT_REDUCTIONS",
            "PROMPT_VERSION",
            "TrajectoryClassifier",
            "has_tool_calls",
        ),
        "classifier",
    ),
    **dict.fromkeys(
        (
            "DEFAULT_REQUESTS_PER_MINUTE",
            "DEFAULT_TOKENS_PER_MINUTE",
            "ClassificationRateLimiter",
        ),
        "rate_limit",
    ),
    **dict.fromkeys(
        (
            "PROJECTION_NAME",
            "TRUNCATION_MARKER",
            "UserInputProjection",
            "project_user_input",
            "serialize_for_model",
        ),
        "input_projection",
    ),
    **dict.fromkeys(("GENERIC_SUBJECT_LABEL", "ScenarioTaxonomy"), "taxonomy"),
    **dict.fromkeys(
        ("LabelJobError", "LabelJobResult", "run_local_label_job", "run_s3_label_job"),
        "jobs",
    ),
    "ChatCompletionsClient": "client",
}
__all__ = list(_EXPORTS) + ["PROJECTION_VERSION", "USER_INPUT_TRUNCATION_STRATEGY"]


def __getattr__(name: str):
    if name in {"PROJECTION_VERSION", "USER_INPUT_TRUNCATION_STRATEGY"}:
        module = import_module(".input_projection", __name__)
        return getattr(
            module, "VERSION" if name == "PROJECTION_VERSION" else "TRUNCATION_STRATEGY"
        )
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module("." + _EXPORTS[name], __name__), name)
