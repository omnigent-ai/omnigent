"""Desync-recovery tests for :class:`ExecutorAdapter` (omnigent issue #1026).

Covers the harness-subprocess half of the cross-process lifecycle-desync fix:

- P0.1 compare-and-clear of the active turn slot (a stale ``run_turn`` finally
  must not clobber a newer turn's ctx).
- P0.2 detached, bounded abnormal-exit ``interrupt_session`` (an abandoned
  inner generation is interrupted exactly once on a non-clean exit, and never
  on a clean one).
- P1.8 tiered orphan-callback watchdog (N consecutive orphans force exactly
  one Tier-1 SDK reset; the counter resets at turn start).
- P2.10 orphaned tool callbacks safe-fail with a structured desync error and
  never dispatch.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolSpec,
    TurnComplete,
)
from omnigent.runtime.harnesses._executor_adapter import (
    _ORPHAN_RESYNC_THRESHOLD,
    ExecutorAdapter,
)
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.server.schemas import CreateResponseRequest


class _FakeExecutor(Executor):
    """Inner executor stub with observable interrupt / close calls.

    ``run_turn`` optionally fires an ``on_iter`` hook on first iteration
    (used to simulate a newer turn binding mid-flight) and optionally blocks
    on ``block_event`` (used to park a turn so the test can cancel it).
    """

    def __init__(
        self,
        events: list[ExecutorEvent] | None = None,
        *,
        on_iter: Any = None,
        block_event: asyncio.Event | None = None,
    ) -> None:
        self._events = events or []
        self._on_iter = on_iter
        self._block_event = block_event
        self.interrupt_calls: list[str] = []
        self.close_calls = 0
        self.close_session_calls = 0

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        if self._on_iter is not None:
            self._on_iter()
        if self._block_event is not None:
            await self._block_event.wait()
        for event in self._events:
            yield event

    async def interrupt_session(self, session_key: str) -> bool:
        self.interrupt_calls.append(session_key)
        return True

    async def close(self) -> None:
        self.close_calls += 1

    async def close_session(self, session_key: str) -> None:
        del session_key
        self.close_session_calls += 1

    async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
        del session_key, content
        return True


def _ctx(response_id: str) -> TurnContext:
    return TurnContext(
        response_id=response_id,
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )


def _request() -> CreateResponseRequest:
    return CreateResponseRequest(model="agent", input="hi")


async def test_stale_finally_does_not_clear_newer_ctx() -> None:
    """P0.1: a stale turn's finally must not clobber a newer turn's slot."""
    ctx_b = _ctx("resp_B")
    adapter: ExecutorAdapter | None = None

    def _bind_newer() -> None:
        # Simulate turn B binding the slot while turn A is mid-flight.
        assert adapter is not None
        adapter._current_ctx = ctx_b
        adapter._current_agent = "agent_b"

    executor = _FakeExecutor(events=[TurnComplete(response="A done")], on_iter=_bind_newer)
    adapter = ExecutorAdapter(executor_factory=lambda: executor)

    ctx_a = _ctx("resp_A")
    await adapter.run_turn(_request(), ctx_a)

    # Turn A's finally ran its compare-and-clear; because the slot now points
    # at B, it must be LEFT pointing at B (no clobber to None).
    assert adapter._current_ctx is ctx_b
    assert adapter._current_agent == "agent_b"


async def test_abnormal_exit_schedules_interrupt_once() -> None:
    """P0.2: a cancelled turn schedules exactly one bounded interrupt."""
    block = asyncio.Event()
    executor = _FakeExecutor(block_event=block)
    adapter = ExecutorAdapter(executor_factory=lambda: executor)

    ctx = _ctx("resp_cancel")
    task = asyncio.create_task(adapter.run_turn(_request(), ctx))
    # Let run_turn enter the executor loop and park on the block event.
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The finally scheduled a detached interrupt; drain it via on_shutdown.
    await adapter.on_shutdown()
    # Interrupted exactly once, keyed by the adapter's inner session key.
    assert executor.interrupt_calls == [adapter._session_key]
    assert not adapter._bg_tasks


async def test_clean_exit_schedules_no_interrupt() -> None:
    """P0.2: a clean TurnComplete exit schedules no abnormal-exit interrupt."""
    executor = _FakeExecutor(events=[TurnComplete(response="done")])
    adapter = ExecutorAdapter(executor_factory=lambda: executor)

    await adapter.run_turn(_request(), _ctx("resp_clean"))
    # No background interrupt task was created.
    assert not adapter._bg_tasks
    await adapter.on_shutdown()
    assert executor.interrupt_calls == []


async def test_orphan_tool_callback_safe_fails() -> None:
    """P2.10: a stray tool callback after slot clear returns a structured error."""
    executor = _FakeExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    # No active turn: _current_ctx / _current_agent are None.
    result = await adapter._stable_tool_executor("Bash", {"command": "ls"})
    assert result == {
        "error": "no active turn context for tool dispatch",
        "code": "runner_turn_context_desync",
    }
    assert adapter._orphan_callback_count == 1


async def test_watchdog_resync_is_idempotent() -> None:
    """P1.8: a burst of orphans triggers exactly one Tier-1 reset."""
    executor = _FakeExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    adapter._ensure_executor()
    assert adapter._executor is executor

    # Fire 2N concurrent orphan callbacks.
    n = _ORPHAN_RESYNC_THRESHOLD * 2
    await asyncio.gather(*(adapter._stable_tool_executor("Bash", {}) for _ in range(n)))

    # Exactly one Tier-1 reset: the cached executor was dropped and closed once.
    assert adapter._executor is None
    assert executor.close_calls == 1
    assert executor.close_session_calls == 1
    assert adapter._orphan_callback_count == 0
    assert adapter._resyncing is False

    # Counter resets on a clean turn start: a single later orphan does not
    # immediately re-trip the watchdog.
    executor2 = _FakeExecutor(events=[TurnComplete(response="ok")])
    adapter._executor_factory = lambda: executor2
    await adapter.run_turn(_request(), _ctx("resp_after_reset"))
    assert adapter._orphan_callback_count == 0
    assert executor2.interrupt_calls == []


class _InterruptScriptExecutor(_FakeExecutor):
    """Executor whose inline ``interrupt_session`` fails on its first call.

    Models a handled-cancel path where the inline interrupt raises: the
    detached, bounded ``_safe_interrupt`` fallback must still fire.
    """

    async def interrupt_session(self, session_key: str) -> bool:
        self.interrupt_calls.append(session_key)
        if len(self.interrupt_calls) == 1:
            raise RuntimeError("inline interrupt boom")
        return True


async def test_failing_inline_interrupt_falls_back_to_detached(monkeypatch: Any) -> None:
    """P0.2: a failing inline interrupt still triggers the detached fallback.

    ``clean_exit`` is set only AFTER the inline ``interrupt_session`` succeeds,
    so a raising inline interrupt leaves ``clean_exit`` False and the finally
    schedules the bounded ``_safe_interrupt`` — the abandoned generation is
    never left un-interrupted.
    """
    executor = _InterruptScriptExecutor(events=[TextChunk(text="x")])
    adapter = ExecutorAdapter(executor_factory=lambda: executor)

    ctx = _ctx("resp_cancelled_inline")
    # Pre-set cancellation so the first event hits the inline-interrupt branch.
    ctx.cancelled.set()

    with pytest.raises(RuntimeError, match="inline interrupt boom"):
        await adapter.run_turn(_request(), ctx)

    # The finally scheduled the detached fallback because clean_exit stayed
    # False (the inline interrupt raised before it could be set).
    await adapter.on_shutdown()
    # Two interrupts: the failed inline one, then the detached fallback.
    assert len(executor.interrupt_calls) == 2
    assert not adapter._bg_tasks


async def test_policy_callback_orphan_counts_toward_watchdog() -> None:
    """P1.8: missing-context policy callbacks count toward the orphan watchdog.

    A ``None``-slot ``_stable_policy_evaluator`` fail-closes the individual
    call (DENY on TOOL_CALL) AND increments the same consecutive-orphan
    counter, so a generation flushing policy callbacks after its turn ended
    triggers the same Tier-1 SDK reset as orphaned tool callbacks.
    """
    executor = _FakeExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    adapter._ensure_executor()
    assert adapter._executor is executor

    verdicts = [
        await adapter._stable_policy_evaluator("PHASE_TOOL_CALL", {})
        for _ in range(_ORPHAN_RESYNC_THRESHOLD)
    ]

    # Each individual missing-context TOOL_CALL still fails closed.
    assert all(v.action == "POLICY_ACTION_DENY" for v in verdicts)
    # The shared watchdog fired exactly once: cached executor dropped + closed.
    assert adapter._executor is None
    assert executor.close_calls == 1
    assert executor.close_session_calls == 1
    assert adapter._orphan_callback_count == 0
    assert adapter._resyncing is False


async def test_policy_callback_orphan_advisory_phase_allows_and_counts() -> None:
    """P1.8/P1.6: advisory-phase orphan policy callbacks ALLOW but still count."""
    executor = _FakeExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    adapter._ensure_executor()

    verdict = await adapter._stable_policy_evaluator("PHASE_LLM_REQUEST", {})
    assert verdict.action == "POLICY_ACTION_ALLOW"
    # Counted toward the watchdog even though it failed open.
    assert adapter._orphan_callback_count == 1
