"""Heartbeat keep-alive while a human approval is pending.

A turn parked on ctx.elicit emits only heartbeats; heartbeats deliberately do
not count as progress, so the ordinary idle window failed a legitimate human
wait as a wedged turn. With a human wait pending, heartbeats hold the idle
deadline open through the idle-only hook; they must never touch the progress
hook (which also extends the absolute ceiling), so the hard cap still bounds
an approval that is never answered.
"""

from __future__ import annotations

import asyncio

from omnigent.runtime.harnesses._scaffold import HeartbeatEvent, TurnContext
from omnigent.server.schemas import (
    ElicitationRequestParams,
    ElicitationResult,
    OutputTextDeltaEvent,
)


def _make_ctx() -> tuple[TurnContext, list[str]]:
    """A TurnContext with both watchdog hooks recording into one log."""
    ctx = TurnContext(
        response_id="resp_test",
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )
    calls: list[str] = []
    ctx._reset_idle_watchdog = lambda: calls.append("progress")
    ctx._hold_idle_watchdog = lambda: calls.append("hold")
    return ctx, calls


def test_heartbeat_does_not_reset_idle_watchdog_normally() -> None:
    ctx, calls = _make_ctx()

    ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="hi"))
    assert calls == ["progress"], "a real progress event resets the deadline"

    ctx.emit(HeartbeatEvent(type="response.heartbeat"))
    assert calls == ["progress"], "a heartbeat must NOT reset the deadline normally"


def test_heartbeat_holds_idle_watchdog_while_human_wait_pending() -> None:
    ctx, calls = _make_ctx()

    async def _park() -> None:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        ctx._pending_elicitations["elicit_1"] = fut
        ctx._pending_human_waits += 1
        try:
            ctx.emit(HeartbeatEvent(type="response.heartbeat"))
            assert calls == ["hold"], (
                "a heartbeat during a pending human wait holds the idle deadline"
            )
        finally:
            ctx._pending_human_waits -= 1
            ctx._pending_elicitations.pop("elicit_1", None)

    asyncio.run(_park())

    ctx.emit(HeartbeatEvent(type="response.heartbeat"))
    assert calls == ["hold"], "the exception ends with the wait (counter restored)"


def test_pending_wait_heartbeat_never_touches_the_progress_hook() -> None:
    """The progress hook also extends the absolute ceiling, so heartbeats
    during a human wait must go through the idle-only hold hook exclusively —
    otherwise an approval that is never answered would keep the turn alive
    past the hard cap."""
    ctx, calls = _make_ctx()

    ctx._pending_human_waits += 1
    try:
        for _ in range(5):
            ctx.emit(HeartbeatEvent(type="response.heartbeat"))
    finally:
        ctx._pending_human_waits -= 1

    assert calls == ["hold"] * 5, (
        f"pending-wait heartbeats must call only the idle-hold hook, got {calls!r}"
    )


def test_elicit_brackets_the_human_wait_counter() -> None:
    """ctx.elicit increments the counter around its park and restores it after."""
    ctx, _ = _make_ctx()

    async def _drive() -> tuple[int, int]:
        pending_during: list[int] = []
        task = asyncio.create_task(
            ctx.elicit(
                "elicit_2",
                ElicitationRequestParams(mode="form", message="approve?"),
            )
        )
        while not ctx._pending_elicitations:
            await asyncio.sleep(0)
        pending_during.append(ctx._pending_human_waits)

        fut = ctx._pending_elicitations["elicit_2"]
        fut.set_result(ElicitationResult(action="accept", content={"value": "ok"}))
        await task
        return pending_during[0], ctx._pending_human_waits

    during, after = asyncio.run(_drive())
    assert during == 1, "counter is 1 while elicit is parked"
    assert after == 0, "counter restored after the reply"
