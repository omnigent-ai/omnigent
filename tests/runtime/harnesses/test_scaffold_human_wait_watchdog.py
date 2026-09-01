"""Heartbeat-driven idle reset while a human approval is pending (#4854).

A turn parked on ctx.elicit emits only heartbeats; heartbeats deliberately do
not reset the idle watchdog, so the ordinary window failed a legitimate human
wait as a wedged turn. With a human wait pending, heartbeats DO reset the
deadline; the absolute ceiling remains the hard cap.
"""

from __future__ import annotations

import asyncio

from omnigent.runtime.harnesses._scaffold import HeartbeatEvent, TurnContext
from omnigent.server.schemas import OutputTextDeltaEvent


def _make_ctx() -> tuple[TurnContext, list[int]]:
    ctx = TurnContext(
        response_id="resp_test",
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )
    resets: list[int] = []

    def _reset() -> None:
        resets.append(len(resets))

    ctx._reset_idle_watchdog = _reset
    return ctx, resets


def test_heartbeat_does_not_reset_idle_watchdog_normally() -> None:
    ctx, resets = _make_ctx()

    ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="hi"))
    assert len(resets) == 1, "a real progress event resets the deadline"

    ctx.emit(HeartbeatEvent(type="response.heartbeat"))
    assert len(resets) == 1, "a heartbeat must NOT reset the deadline normally"


def test_heartbeat_resets_idle_watchdog_while_human_wait_pending() -> None:
    ctx, resets = _make_ctx()

    async def _park() -> None:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        ctx._pending_elicitations["elicit_1"] = fut
        ctx._pending_human_waits += 1
        try:
            ctx.emit(HeartbeatEvent(type="response.heartbeat"))
            assert len(resets) == 1, "heartbeat resets the deadline while a human wait is pending"
        finally:
            ctx._pending_human_waits -= 1
            ctx._pending_elicitations.pop("elicit_1", None)

    asyncio.run(_park())

    ctx.emit(HeartbeatEvent(type="response.heartbeat"))
    assert len(resets) == 1, "the exception ends with the wait (counter restored)"


def test_elicit_brackets_the_human_wait_counter() -> None:
    """ctx.elicit increments the counter around its park and restores it after."""
    ctx, _ = _make_ctx()
    from omnigent.server.schemas import ElicitationRequestParams

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
        from omnigent.server.schemas import ElicitationResult

        fut.set_result(ElicitationResult(action="accept", content={"value": "ok"}))
        await task
        return pending_during[0], ctx._pending_human_waits

    during, after = asyncio.run(_drive())
    assert during == 1, "counter is 1 while elicit is parked"
    assert after == 0, "counter restored after the reply"
