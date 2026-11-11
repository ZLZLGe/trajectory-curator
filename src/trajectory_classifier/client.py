"""Minimal OpenAI-compatible Chat Completions client with bounded retries."""

from __future__ import annotations

import http.client
import json
import logging
import math
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import orjson

from .rate_limit import ClassificationRateLimiter, estimate_request_tokens

DEFAULT_MAX_OUTPUT_TOKENS = 4_096
LOGGER = logging.getLogger(__name__)


class ClassificationAPIError(RuntimeError):
    """Base class for safe-to-display classifier API failures."""


class ClassificationConfigurationError(ClassificationAPIError):
    """A request cannot succeed until API configuration is corrected."""


class ClassificationRequestError(ClassificationAPIError):
    """A single classification request failed permanently."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ClassificationRetryExhausted(ClassificationAPIError):
    """A retryable API failure persisted through the configured attempts."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ClassificationContextLimitError(ClassificationAPIError):
    """The gateway rejected a request because its context is too large."""


def _is_context_limit_error(error: urllib.error.HTTPError) -> bool:
    if error.code == 413:
        return True
    try:
        detail = error.read(8192).decode("utf-8", errors="ignore").lower()
    except (OSError, http.client.IncompleteRead):
        # The status code remains authoritative if its explanatory body is cut
        # short. Do not let a read failure bypass the caller's HTTP handling.
        return False
    if not detail:
        return False
    markers = ("exceed", "maximum", "max_", "limit", "too long", "length")
    return ("context" in detail or "token" in detail or "prompt" in detail) and any(
        marker in detail for marker in markers
    )


def _validate_api_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ClassificationConfigurationError("classifier API URL is required")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ClassificationConfigurationError(
            "classifier API URL must be an absolute HTTP(S) URL"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ClassificationConfigurationError(
            "classifier API URL must not contain credentials"
        )
    if not parsed.path.rstrip("/").endswith("/chat/completions"):
        raise ClassificationConfigurationError(
            "classifier API URL must include the Chat Completions path"
        )
    if parsed.query or parsed.fragment:
        raise ClassificationConfigurationError(
            "classifier API URL must not contain a query or fragment"
        )
    return value


def _retry_after(headers: Message | None) -> float | None:
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return min(max(seconds, 0.0), 300.0) if math.isfinite(seconds) else None


class ChatCompletionsClient:
    """Call one configured OpenAI-compatible model without logging secrets."""

    def __init__(
        self,
        *,
        api_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float = 120.0,
        max_retries: int = 5,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        requests_per_minute: int | None = None,
        tokens_per_minute: int | None = None,
        rate_limit_state_path: str | Path | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.api_url = _validate_api_url(api_url)
        if not isinstance(model, str) or not model:
            raise ClassificationConfigurationError("classifier model is required")
        if not isinstance(api_key, str) or not api_key:
            raise ClassificationConfigurationError("classifier API key is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        self.model = model
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self._opener = opener
        self._sleeper = sleeper
        self._jitter = jitter
        if (requests_per_minute is None) != (tokens_per_minute is None):
            raise ValueError("RPM and TPM limits must be configured together")
        if rate_limit_state_path is not None and requests_per_minute is None:
            raise ValueError("shared rate limit state requires RPM and TPM limits")
        self.rate_limiter = (
            None
            if requests_per_minute is None
            else ClassificationRateLimiter(
                requests_per_minute=requests_per_minute,
                tokens_per_minute=tokens_per_minute,
                state_path=rate_limit_state_path,
            )
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(api_url={self.api_url!r}, "
            f"model={self.model!r}, api_key='[REDACTED]')"
        )

    def complete(self, messages: list[dict[str, str]]) -> str:
        """Return assistant content, retrying only transient failures."""

        payload = orjson.dumps(
            {
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": self.max_output_tokens,
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
            }
        )
        request = urllib.request.Request(
            self.api_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        estimated_tokens = estimate_request_tokens(payload, self.max_output_tokens)
        for attempt in range(self.max_retries + 1):
            try:
                raw = self._request_once(request, estimated_tokens)
                return self._parse_response(raw)
            except urllib.error.HTTPError as error:
                status = error.code
                if status in {400, 413, 422} and _is_context_limit_error(error):
                    error.close()
                    raise ClassificationContextLimitError from error
                if status == 404:
                    retry_after = _retry_after(error.headers)
                    error.close()
                    if attempt == self.max_retries:
                        raise ClassificationConfigurationError(
                            "classifier API configuration failed with HTTP 404"
                        ) from error
                    LOGGER.warning(
                        "classifier API HTTP failure; retrying "
                        "retry_attempt=%d/%d status=%d",
                        attempt + 1,
                        self.max_retries,
                        status,
                    )
                    self._sleep_before_retry(attempt, retry_after)
                    continue
                error.close()
                if status in {401, 403, 422}:
                    raise ClassificationConfigurationError(
                        f"classifier API configuration failed with HTTP {status}"
                    ) from error
                if status == 429 or 500 <= status < 600:
                    retry_after = _retry_after(error.headers)
                    if attempt == self.max_retries:
                        if status == 429:
                            self._defer_rate_limit(
                                self._retry_delay(attempt, retry_after)
                            )
                        reason = (
                            "rate_limit_exhausted"
                            if status == 429
                            else "service_retry_exhausted"
                        )
                        raise ClassificationRetryExhausted(reason) from error
                    self._sleep_before_retry(
                        attempt, retry_after, shared_cooldown=status == 429
                    )
                    continue
                raise ClassificationRequestError(f"classifier_http_{status}") from error
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.IncompleteRead,
            ) as error:
                # urllib can expose these directly while opening or reading a
                # response. ConnectionError includes RemoteDisconnected, reset,
                # abort, and broken pipe; do not catch unrelated OSError or all
                # HTTPException subclasses as transient network failures.
                if attempt == self.max_retries:
                    LOGGER.warning(
                        "classifier API network retries exhausted; "
                        "attempts=%d error_type=%s",
                        attempt + 1,
                        type(error).__name__,
                    )
                    # Raw transport errors may contain URLs, credentials, or
                    # response fragments. Keep them out of displayed tracebacks.
                    raise ClassificationRetryExhausted(
                        "network_retry_exhausted"
                    ) from None
                LOGGER.warning(
                    "classifier API network failure; retrying "
                    "retry_attempt=%d/%d error_type=%s",
                    attempt + 1,
                    self.max_retries,
                    type(error).__name__,
                )
                self._sleep_before_retry(attempt, None)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                if attempt == self.max_retries:
                    raise ClassificationRetryExhausted(
                        "invalid_api_response"
                    ) from error
                self._sleep_before_retry(attempt, None)

        raise AssertionError("retry loop must return or raise")

    def _request_once(
        self, request: urllib.request.Request, estimated_tokens: int
    ) -> bytes:
        reservation = None
        if self.rate_limiter is not None:
            try:
                reservation = self.rate_limiter.acquire(
                    estimated_tokens, timeout_seconds=self.timeout_seconds
                )
            except (OSError, ValueError):
                raise ClassificationConfigurationError(
                    "classifier rate budget unavailable, invalid, or too small "
                    "for the estimated request"
                ) from None
        actual_tokens = None
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
            actual_tokens = self._response_tokens(raw)
            return raw
        finally:
            if reservation is not None:
                try:
                    self.rate_limiter.settle(reservation, actual_tokens)
                except (OSError, ValueError):
                    raise ClassificationConfigurationError(
                        "classifier rate budget could not record request usage"
                    ) from None

    @staticmethod
    def _response_tokens(raw: bytes) -> int | None:
        try:
            value = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return None
        usage = value.get("usage") if type(value) is dict else None
        if type(usage) is not dict:
            return None
        total = usage.get("total_tokens")
        if type(total) is int and total >= 0:
            return total
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if all(type(v) is int and v >= 0 for v in (prompt, completion)):
            return prompt + completion
        return None

    @staticmethod
    def _parse_response(raw: bytes) -> str:
        value = orjson.loads(raw)
        if type(value) is not dict:
            raise ValueError("response must be an object")
        choices = value.get("choices")
        if type(choices) is not list or not choices or type(choices[0]) is not dict:
            raise ValueError("response must contain choices")
        message = choices[0].get("message")
        if type(message) is not dict:
            raise ValueError("response choice must contain a message")
        content = message.get("content")
        if type(content) is not str or not content:
            raise ValueError("response message must contain text content")
        return content

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        return min(60.0, 2.0**attempt) * (0.5 + 0.5 * self._jitter())

    def _defer_rate_limit(self, seconds: float) -> None:
        if self.rate_limiter is not None:
            try:
                self.rate_limiter.defer(seconds)
            except (OSError, ValueError):
                raise ClassificationConfigurationError(
                    "classifier rate budget could not record the server cooldown"
                ) from None

    def _sleep_before_retry(
        self,
        attempt: int,
        retry_after: float | None,
        *,
        shared_cooldown: bool = False,
    ) -> None:
        delay = self._retry_delay(attempt, retry_after)
        if shared_cooldown:
            self._defer_rate_limit(delay)
        self._sleeper(delay)


__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "ChatCompletionsClient",
    "ClassificationAPIError",
    "ClassificationConfigurationError",
    "ClassificationContextLimitError",
    "ClassificationRequestError",
    "ClassificationRetryExhausted",
]
