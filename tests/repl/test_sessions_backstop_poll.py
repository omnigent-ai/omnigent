"""Tests for the REPL turn-completion backstop poll.

``_SessionsChatReplAdapter.send`` waits for the turn's terminal
``session.status`` event, which ``_stream_pump`` delivers. Because the
pub-sub has no replay (and httpx's ASGI transport may not have the
subscription active yet), the adapter also polls
``GET /v1/sessions/{id}`` as a backstop.

That snapshot is expensive and scales with conversation history, so the
poll used to cost roughly one request per second of turn wall-clock —
several times the dispatch work of the message that started the turn.
These tests pin the three properties that keep it cheap *and* still a
backstop, using request counts and a lower bound on elapsed time so
they stay deterministic on a loaded box:

1. A stream that is delivering events costs zero snapshots.
2. Consecutive quiet snapshots back off instead of ticking at 1 Hz.
3. A silent stream still ends the turn from the snapshot.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.repl import _repl
from omnigent.repl._repl import _SessionsChatReplAdapter

pytestmark = pytest.mark.asyncio

_SESSION_ID = "conv_backstop"


@dataclass
class _StubSnapshot:
    """Minimal snapshot shape: the backstop only reads ``status``."""

    status: str


def _build_adapter(statuses: list[str]) -> tuple[_SessionsChatReplAdapter, list[float]]:
    """
    Build an adapter whose ``sessions.get`` is counted and timestamped.

    :param statuses: Statuses to return from successive snapshot GETs;
        the last one repeats if the backstop polls more times.
    :returns: The adapter and the list that records a monotonic
        timestamp per ``sessions.get`` call.
    """
    calls: list[float] = []

    async def _get(session_id: str) -> _StubSnapshot:
        assert session_id == _SESSION_ID
        calls.append(time.monotonic())
        return _StubSnapshot(status=statuses[min(len(calls) - 1, len(statuses) - 1)])

    client = MagicMock()
    client.sessions.get = AsyncMock(side_effect=_get)
    adapter = _SessionsChatReplAdapter(
        client=client,
        agent_name="test-agent",
        session_id=_SESSION_ID,
    )
    adapter._turn_done = asyncio.Event()
    return adapter, calls


async def test_live_stream_costs_no_backstop_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that delivered an event recently is never snapshotted.

    The pump owns turn completion whenever the subscription is live, so
    every backstop wake during a streaming turn must skip the GET. This
    is the case the old fixed cadence paid for on every turn.
    """
    monkeypatch.setattr(_repl, "_BACKSTOP_MIN_INTERVAL_S", 0.01)
    monkeypatch.setattr(_repl, "_BACKSTOP_MAX_INTERVAL_S", 0.01)
    # Wide quiet window: one stamped event proves the stream live for the
    # whole test, so scheduling jitter can't fake a stream stall.
    monkeypatch.setattr(_repl, "_BACKSTOP_STREAM_QUIET_S", 30.0)

    adapter, calls = _build_adapter(["running"])
    adapter._last_stream_event_at = time.monotonic()

    async def _finish_turn() -> None:
        await asyncio.sleep(0.15)
        adapter._turn_done.set()

    finisher = asyncio.create_task(_finish_turn())
    await adapter._await_turn_done(_SESSION_ID)
    await finisher

    # ~15 backstop wakes at a 0.01s interval, zero requests.
    assert calls == []


async def test_quiet_stream_backs_off_instead_of_ticking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive quiet snapshots double the wait up to the cap.

    Asserted as a lower bound on elapsed time: the backed-off schedule
    (1x + 2x + 4x + 8x-capped) cannot fit inside four fixed-interval
    ticks, and load noise only inflates the measured span.
    """
    monkeypatch.setattr(_repl, "_BACKSTOP_MIN_INTERVAL_S", 0.05)
    monkeypatch.setattr(_repl, "_BACKSTOP_MAX_INTERVAL_S", 0.40)

    # Never stamped, so the stream reads as quiet from the first wake.
    adapter, calls = _build_adapter(["running", "running", "running", "idle"])
    started = time.monotonic()
    await adapter._await_turn_done(_SESSION_ID)
    elapsed = time.monotonic() - started

    assert len(calls) == 4
    # Backed off: 0.05 + 0.10 + 0.20 + 0.40 = 0.75s. A 0.05s fixed
    # cadence would have reached the fourth poll by 0.20s.
    assert elapsed >= 0.70, f"backstop did not back off (elapsed {elapsed:.3f}s)"


@pytest.mark.parametrize("terminal_status", ["idle", "failed"])
async def test_silent_stream_still_ends_turn_from_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: str,
) -> None:
    """The backstop still catches a terminal state the stream missed.

    This is the case the poll exists for — a terminal event published
    into a reconnect gap, or a subscription that never became active —
    so the cheaper cadence must not turn into no backstop at all.
    """
    monkeypatch.setattr(_repl, "_BACKSTOP_MIN_INTERVAL_S", 0.01)

    adapter, calls = _build_adapter([terminal_status])
    await adapter._await_turn_done(_SESSION_ID)

    assert len(calls) == 1
    assert adapter._turn_done.is_set()
