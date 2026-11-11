"""Reserve RPM and TPM budgets before HTTP attempts, across optional shared files."""

from __future__ import annotations

import fcntl
import logging
import math
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import orjson

DEFAULT_REQUESTS_PER_MINUTE = 90
DEFAULT_TOKENS_PER_MINUTE = 4_500_000
TOKEN_ESTIMATION = "utf8_bytes_plus_output_v1"
WINDOW_SECONDS = 60.0
LOGGER = logging.getLogger(__name__)


def estimate_request_tokens(payload: bytes, max_output_tokens: int) -> int:
    """Use UTF-8 request bytes as a conservative estimate, plus framing and output.

    This deliberately avoids applying a short sample's chars/token ratio to new
    inputs. A byte-based BPE tokenizer uses no more tokens than its input bytes;
    256 extra tokens cover the chat template that is not in the JSON payload.
    """

    return len(payload) + 256 + max_output_tokens


class ClassificationRateLimiter:
    """Thread-safe rolling budgets with optional POSIX locking across Workers.

    Request slots expire 60 seconds after sending. Token reservations remain
    while in flight and for 60 seconds after completion. Unknown/failed usage
    keeps its conservative reservation. An abandoned request expires after at
    least ten minutes so a crashed process cannot reserve the budget forever.
    """

    def __init__(
        self,
        *,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        tokens_per_minute: int = DEFAULT_TOKENS_PER_MINUTE,
        state_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(requests_per_minute) is not int or requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be a positive integer")
        if type(tokens_per_minute) is not int or tokens_per_minute <= 0:
            raise ValueError("tokens_per_minute must be a positive integer")
        self.requests_per_minute = requests_per_minute
        self.tokens_per_minute = tokens_per_minute
        self.state_path = (
            None if state_path is None else Path(state_path).expanduser().resolve()
        )
        if self.state_path is not None and not self.state_path.parent.is_dir():
            raise ValueError("rate limit state parent directory must exist")
        self._clock = clock
        self._sleeper = sleeper
        self._mutex = threading.Lock()
        self._memory = self._empty_state()

    @property
    def config(self) -> dict[str, Any]:
        return {
            "requests_per_minute": self.requests_per_minute,
            "tokens_per_minute": self.tokens_per_minute,
            "scope": "shared_file" if self.state_path is not None else "client",
            "token_estimation": TOKEN_ESTIMATION,
            "window_seconds": WINDOW_SECONDS,
            "token_accounting": "in_flight_and_60s_after_completion",
        }

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "limits": [self.requests_per_minute, self.tokens_per_minute],
            "next_start": 0.0,
            "cooldown": 0.0,
            "entries": [],
        }

    def _validate_state(self, state: object) -> dict[str, Any]:
        def number(value: object) -> bool:
            return type(value) in (int, float) and math.isfinite(value) and value >= 0

        if (
            type(state) is not dict
            or state.get("version") != 1
            or type(state.get("limits")) is not list
            or len(state["limits"]) != 2
            or any(type(v) is not int or v <= 0 for v in state["limits"])
            or not number(state.get("next_start"))
            or not number(state.get("cooldown"))
            or type(state.get("entries")) is not list
        ):
            raise ValueError("invalid shared classifier rate limit state")
        for entry in state["entries"]:
            if (
                type(entry) is not dict
                or type(entry.get("id")) is not str
                or len(entry["id"]) != 32
                or not number(entry.get("start"))
                or not number(entry.get("until"))
                or type(entry.get("tokens")) is not int
                or entry["tokens"] < 0
            ):
                raise ValueError("invalid shared classifier rate limit reservation")
        now = self._clock()
        state["entries"] = [
            row
            for row in state["entries"]
            if row["start"] + WINDOW_SECONDS > now or row["until"] > now
        ]
        if state["limits"] != self._memory["limits"]:
            if state["entries"] or state["cooldown"] > now or state["next_start"] > now:
                raise ValueError("active shared classifier rate limits do not match")
            state = self._empty_state()
        return state

    @contextmanager
    def _transaction(self) -> Iterator[dict[str, Any]]:
        with self._mutex:
            if self.state_path is None:
                state = self._validate_state(self._memory)
                yield state
                self._memory = state
                return
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(descriptor, "rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    try:
                        raw = self.state_path.read_bytes()
                    except FileNotFoundError:
                        state = self._empty_state()
                    else:
                        try:
                            state = self._validate_state(orjson.loads(raw))
                        except orjson.JSONDecodeError:
                            raise ValueError(
                                "invalid shared classifier rate limit JSON"
                            ) from None
                    yield state
                    # Separate lock inode plus atomic replacement prevents a
                    # process crash during write from truncating the live state.
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            dir=self.state_path.parent,
                            prefix=".classifier-rate-limit-",
                            delete=False,
                        ) as stream:
                            temporary = Path(stream.name)
                            stream.write(orjson.dumps(state))
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, self.state_path)
                    finally:
                        if temporary is not None:
                            temporary.unlink(missing_ok=True)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def acquire(self, tokens: int, *, timeout_seconds: float = 180.0) -> str:
        if type(tokens) is not int or tokens <= 0:
            raise ValueError("reserved tokens must be a positive integer")
        if tokens > self.tokens_per_minute:
            raise ValueError(
                "estimated request exceeds TPM budget; reduce max_context_chars "
                "or increase the token budget within the user's quota"
            )
        waiting_logged = False
        while True:
            with self._transaction() as state:
                now = self._clock()
                entries = state["entries"]
                delay = max(0.0, state["next_start"] - now, state["cooldown"] - now)
                starts = [r["start"] for r in entries if r["start"] + 60 > now]
                if len(starts) >= self.requests_per_minute:
                    delay = max(delay, min(starts) + WINDOW_SECONDS - now)
                charged = [r for r in entries if r["until"] > now]
                if sum(r["tokens"] for r in charged) + tokens > self.tokens_per_minute:
                    delay = max(delay, min(r["until"] for r in charged) - now)
                if delay <= 0:
                    reservation = uuid.uuid4().hex
                    entries.append(
                        {
                            "id": reservation,
                            "start": now,
                            "until": now + max(600.0, timeout_seconds * 2 + 60),
                            "tokens": tokens,
                        }
                    )
                    state["next_start"] = (
                        now + WINDOW_SECONDS / self.requests_per_minute
                    )
                    return reservation
            # Do not hold the process or file lock while waiting. Recheck often:
            # in-flight responses may release unused output/token reservations.
            if not waiting_logged:
                LOGGER.info(
                    "classifier waiting for shared request/token budget; "
                    "estimated_tokens=%d recheck_seconds=%.3f",
                    tokens,
                    min(delay + 0.001, 5.0),
                )
                waiting_logged = True
            self._sleeper(min(delay + 0.001, 5.0))

    def settle(self, reservation: str, actual_tokens: int | None) -> None:
        if actual_tokens is not None and (
            type(actual_tokens) is not int or actual_tokens < 0
        ):
            raise ValueError("actual tokens must be a non-negative integer or None")
        with self._transaction() as state:
            now = self._clock()
            for entry in state["entries"]:
                if entry["id"] == reservation:
                    if actual_tokens is not None:
                        entry["tokens"] = actual_tokens
                    entry["until"] = now + WINDOW_SECONDS
                    return
            # A request outlived its abandoned-request lease. Do not silently
            # discard known usage, even though this should exceed normal timeouts.
            if actual_tokens is not None:
                state["entries"].append(
                    {
                        "id": reservation,
                        "start": max(0.0, now - WINDOW_SECONDS),
                        "until": now + WINDOW_SECONDS,
                        "tokens": actual_tokens,
                    }
                )

    def defer(self, seconds: float) -> None:
        """Apply a server 429 cooldown to every participant in the same budget."""

        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("rate limit cooldown must be finite and non-negative")
        with self._transaction() as state:
            state["cooldown"] = max(state["cooldown"], self._clock() + seconds)
