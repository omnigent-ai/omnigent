"""Tests for bounded retry of transient HTTP 429 throttles."""

from __future__ import annotations

import httpx
import pytest

from omnigent.native import transient_429
from omnigent.native.transient_429 import (
    TRANSIENT_429_MAX_BACKOFF_S,
    retry_after_hint_s,
    send_with_transient_429_retry,
)


def _resp(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers, request=httpx.Request("POST", "https://ap/x"))


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("2", 2.0),
        ("0", 0.0),
        ("9999", TRANSIENT_429_MAX_BACKOFF_S),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("-5", None),
        (None, None),
    ],
)
def test_retry_after_hint_parses_numeric_and_caps(
    header: str | None, expected: float | None
) -> None:
    """Numeric hints are honoured up to the max backoff; other forms are ignored."""
    headers = {"Retry-After": header} if header is not None else None
    assert retry_after_hint_s(_resp(429, headers)) == expected


@pytest.mark.asyncio
async def test_non_429_responses_return_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success and non-429 errors pass through on the first attempt, no sleep."""
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(transient_429, "_sleep", _sleep)
    for status in (200, 404, 409, 500):
        attempts: list[int] = []

        async def _send(status: int = status, attempts: list[int] = attempts) -> httpx.Response:
            attempts.append(status)
            return _resp(status)

        resp = await send_with_transient_429_retry(_send)
        assert resp.status_code == status
        assert len(attempts) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_transient_429_is_retried_honouring_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-off 429 is retried after max(backoff, Retry-After) and then succeeds."""
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(transient_429, "_sleep", _sleep)
    responses = [_resp(429, {"Retry-After": "3"}), _resp(200)]

    async def _send() -> httpx.Response:
        return responses.pop(0)

    resp = await send_with_transient_429_retry(_send)
    assert resp.status_code == 200
    assert sleeps == [3.0], "Retry-After above the initial backoff sets the wait"


@pytest.mark.asyncio
async def test_persistent_429_returns_last_response_once_budget_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that keeps throttling surfaces its 429 to the caller, bounded."""
    clock = {"t": 0.0}
    monkeypatch.setattr(transient_429, "_monotonic", lambda: clock["t"])

    async def _sleep(seconds: float) -> None:
        clock["t"] += seconds

    monkeypatch.setattr(transient_429, "_sleep", _sleep)
    attempts: list[float] = []

    async def _send() -> httpx.Response:
        attempts.append(clock["t"])
        return _resp(429)

    resp = await send_with_transient_429_retry(_send)
    assert resp.status_code == 429
    assert 2 <= len(attempts) <= 8, "retried within the budget, then gave up"
