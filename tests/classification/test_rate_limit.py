from __future__ import annotations

import io
import time
import urllib.error
from concurrent.futures import ProcessPoolExecutor
from itertools import pairwise

import orjson
import pytest

from trajectory_classifier.client import (
    ChatCompletionsClient,
    ClassificationConfigurationError,
)
from trajectory_classifier.rate_limit import (
    ClassificationRateLimiter,
    estimate_request_tokens,
)


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def limiter(clock, *, rpm=90, tpm=1000, path=None):
    return ClassificationRateLimiter(
        requests_per_minute=rpm,
        tokens_per_minute=tpm,
        state_path=path,
        clock=clock,
        sleeper=clock.sleep,
    )


def test_requests_are_paced_even_when_responses_are_fast():
    clock = Clock()
    budget = limiter(clock)
    starts = []
    for _ in range(100):
        budget.settle(budget.acquire(1), 1)
        starts.append(clock())
    assert all(b - a >= 60 / 90 - 1e-8 for a, b in pairwise(starts))
    assert (
        max(sum(start <= other < start + 60 for other in starts) for start in starts)
        <= 90
    )


def test_long_input_waits_for_rolling_token_budget():
    clock = Clock()
    budget = limiter(clock)
    budget.settle(budget.acquire(600), 600)
    second = budget.acquire(600)
    assert clock() >= 1060
    budget.settle(second, 600)


def test_inflight_reservations_and_usage_reconciliation():
    clock = Clock()
    budget = limiter(clock)
    first = budget.acquire(900)
    clock.now += 20
    # Release the conservative estimate after measuring a much smaller usage.
    budget.settle(first, 100)
    second = budget.acquire(800)
    assert clock() == 1020
    budget.settle(second, 800)
    budget.acquire(800)
    assert clock() >= 1080  # Tokens are held until 60 seconds after completion.


def test_missing_usage_keeps_reservation_and_crashed_requests_expire():
    clock = Clock()
    budget = limiter(clock)
    budget.settle(budget.acquire(600), None)
    budget.acquire(600)  # This second request is abandoned while in flight.
    assert clock() >= 1060
    budget.acquire(600)
    assert clock() >= 1660


def test_actual_usage_larger_than_estimate_is_charged():
    clock = Clock()
    budget = limiter(clock)
    budget.settle(budget.acquire(100), 950)
    budget.acquire(100)
    assert clock() >= 1060


def test_shared_file_coordinates_independent_clients_and_cooldown(tmp_path):
    clock = Clock()
    path = tmp_path / "quota.json"
    first, second = limiter(clock, path=path), limiter(clock, path=path)
    first.settle(first.acquire(600), 600)
    second.acquire(600)
    assert clock() >= 1060
    first.defer(13)
    second.acquire(1)
    assert clock() >= 1073
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.with_name("quota.json.lock").stat().st_mode & 0o777 == 0o600


class BudgetWouldWait(Exception):
    pass


def _competing_process(path):
    def bounded_sleep(seconds):
        if seconds > 0.01:
            raise BudgetWouldWait
        time.sleep(seconds)

    budget = ClassificationRateLimiter(
        requests_per_minute=1_000_000,
        tokens_per_minute=100,
        state_path=path,
        sleeper=bounded_sleep,
    )
    try:
        budget.acquire(40)
    except BudgetWouldWait:
        return False
    return True


def test_file_lock_prevents_overspend_across_processes(tmp_path):
    path = tmp_path / "shared.json"
    with ProcessPoolExecutor(max_workers=4) as pool:
        allowed = list(pool.map(_competing_process, [path] * 12))
    assert sum(allowed) == 2
    state = orjson.loads(path.read_bytes())
    assert sum(row["tokens"] for row in state["entries"]) == 80
    assert len(state["entries"]) == 2


@pytest.mark.parametrize("invalid", [b"", b"{", b"{}", b'{"version":2}'])
def test_corrupt_shared_state_fails_closed(tmp_path, invalid):
    path = tmp_path / "bad.json"
    path.write_bytes(invalid)
    with pytest.raises(ValueError, match="invalid shared"):
        limiter(Clock(), path=path).acquire(10)
    assert path.read_bytes() == invalid


def test_conflicting_live_limits_are_rejected_then_reset_when_idle(tmp_path):
    clock = Clock()
    path = tmp_path / "limits.json"
    first = limiter(clock, path=path)
    first.settle(first.acquire(1), 1)
    changed = limiter(clock, rpm=80, path=path)
    with pytest.raises(ValueError, match="do not match"):
        changed.acquire(1)
    clock.now += 61
    changed.acquire(1)
    assert orjson.loads(path.read_bytes())["limits"] == [80, 1000]


@pytest.mark.parametrize("rpm,tpm", [(0, 10), (10, 0), (True, 10), (10, 1.2)])
def test_invalid_limits_are_rejected(rpm, tpm):
    with pytest.raises(ValueError):
        limiter(Clock(), rpm=rpm, tpm=tpm)


def test_oversize_single_request_fails_without_waiting():
    clock = Clock()
    with pytest.raises(ValueError, match="exceeds TPM"):
        limiter(clock).acquire(1001)
    assert not clock.sleeps


def test_estimation_covers_unicode_and_the_entire_payload():
    payload = orjson.dumps({"messages": [{"role": "user", "content": "中文😀"}]})
    assert estimate_request_tokens(payload, 4096) > len(payload.decode()) + 4096
    assert (
        estimate_request_tokens(payload + b"x", 4096)
        == estimate_request_tokens(payload, 4096) + 1
    )


def test_real_client_counts_retry_and_records_usage(tmp_path):
    clock = Clock()
    budget = limiter(clock, tpm=10000, path=tmp_path / "client.json")
    calls = []

    def opener(request, **_kwargs):
        calls.append((clock(), request.data))
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 429, "limited", {"Retry-After": "13"}, io.BytesIO()
            )
        return io.BytesIO(
            orjson.dumps(
                {
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 123, "completion_tokens": 7},
                }
            )
        )

    client = ChatCompletionsClient(
        api_url="https://classifier.invalid/v1/chat/completions",
        model="test",
        api_key="never-save-this",
        max_output_tokens=100,
        max_retries=20,
        opener=opener,
        sleeper=clock.sleep,
    )
    client.rate_limiter = budget
    assert client.complete([{"role": "user", "content": "hello"}]) == '{"ok": true}'
    assert len(calls) == 2
    assert calls[1][0] - calls[0][0] >= 13
    assert calls[0][1] == calls[1][1]
    state = orjson.loads((tmp_path / "client.json").read_bytes())
    assert len(state["entries"]) == 2  # The rejected attempt still consumes a slot.
    assert state["entries"][0]["tokens"] > 130  # Unknown 429 usage stays reserved.
    assert state["entries"][1]["tokens"] == 130
    assert b"never-save-this" not in (tmp_path / "client.json").read_bytes()


def test_network_retry_retains_unknown_usage_and_also_reserves_a_new_slot(tmp_path):
    clock = Clock()
    path = tmp_path / "network.json"
    budget = limiter(clock, tpm=10000, path=path)
    calls = []

    def opener(_request, **_kwargs):
        calls.append(clock())
        if len(calls) == 1:
            raise ConnectionError("transport failed")
        return io.BytesIO(orjson.dumps({"choices": [{"message": {"content": "ok"}}]}))

    client = ChatCompletionsClient(
        api_url="https://classifier.invalid/v1/chat/completions",
        model="test",
        api_key="secret",
        max_output_tokens=100,
        max_retries=1,
        opener=opener,
        sleeper=clock.sleep,
        jitter=lambda: 0,
    )
    client.rate_limiter = budget
    assert client.complete([{"role": "user", "content": "hello"}]) == "ok"
    assert len(orjson.loads(path.read_bytes())["entries"]) == 2
    assert calls[1] - calls[0] >= 60 / 90


def test_budget_failure_cannot_turn_into_unthrottled_http(tmp_path):
    path = tmp_path / "broken.json"
    path.write_bytes(b"invalid")
    calls = []
    client = ChatCompletionsClient(
        api_url="https://classifier.invalid/v1/chat/completions",
        model="test",
        api_key="secret",
        requests_per_minute=90,
        tokens_per_minute=10000,
        rate_limit_state_path=path,
        opener=lambda *_args, **_kwargs: calls.append(1),
    )
    with pytest.raises(ClassificationConfigurationError, match="rate budget"):
        client.complete([{"role": "user", "content": "hello"}])
    assert not calls


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"total_tokens": 10}, 10),
        ({"total_tokens": True}, None),
        ({"total_tokens": -1}, None),
        ({"prompt_tokens": 10, "completion_tokens": 3}, 13),
        ({"total_tokens": 0}, 0),
        ({"prompt_tokens": 10}, None),
    ],
)
def test_invalid_or_missing_usage_is_not_treated_as_zero(usage, expected):
    assert (
        ChatCompletionsClient._response_tokens(orjson.dumps({"usage": usage}))
        == expected
    )
