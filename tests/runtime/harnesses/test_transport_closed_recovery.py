"""Definite transport loss reuses the bounded, pre-progress cleanup gate."""

import asyncio
from typing import Any

import pytest

from omnigent.errors import HarnessTransportClosedError
from omnigent.runtime.harnesses import _scaffold
from omnigent.runtime.harnesses._scaffold import HarnessApp, TurnContext
from omnigent.server.schemas import OutputTextDeltaEvent, RetryEvent


@pytest.mark.parametrize(
    ("replay_safe", "progress", "cleanup_complete", "cancel_at_cleanup", "expected_attempts"),
    [
        (True, False, True, False, 2),
        (False, False, True, False, 1),
        (True, True, True, False, 1),
        (True, False, False, False, 1),
        (True, False, True, True, 1),
    ],
)
async def test_transport_recovery_requires_both_progress_guards_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    replay_safe: bool,
    progress: bool,
    cleanup_complete: bool,
    cancel_at_cleanup: bool,
    expected_attempts: int,
) -> None:
    attempts = 0
    cleanups = 0
    queue: asyncio.Queue[Any] = asyncio.Queue()
    ctx = TurnContext(response_id="transport", event_queue=queue, cancelled=asyncio.Event())

    class App(HarnessApp):
        async def run_turn(self, request: Any, context: TurnContext) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                if progress:
                    context.emit(
                        OutputTextDeltaEvent(type="response.output_text.delta", delta="once")
                    )
                raise HarnessTransportClosedError("Codex stdout closed", replay_safe=replay_safe)

        async def _prepare_turn_retry(self) -> bool:
            nonlocal cleanups
            cleanups += 1
            if cancel_at_cleanup:
                ctx.cancelled.set()
            return cleanup_complete

    monkeypatch.setattr(_scaffold, "_TURN_IDLE_TIMEOUT_S", 10.0)
    monkeypatch.setattr(_scaffold, "_TURN_ABSOLUTE_TIMEOUT_S", 120.0)
    monkeypatch.setattr(_scaffold, "_WEDGED_TURN_RECOVERY_RETRIES", 1)
    app = App()
    async with asyncio.timeout(1):
        if expected_attempts == 2:
            await app._guarded_run_turn(None, ctx)  # type: ignore[arg-type]
        else:
            with pytest.raises(HarnessTransportClosedError):
                await app._guarded_run_turn(None, ctx)  # type: ignore[arg-type]
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert attempts == expected_attempts
    assert cleanups == int(replay_safe and not progress)
    assert sum(isinstance(event, RetryEvent) for event in events) == expected_attempts - 1
    assert events[-1] is None


async def test_repeated_transport_closure_exhausts_the_existing_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    cleanups = 0

    class App(HarnessApp):
        async def run_turn(self, request: Any, ctx: TurnContext) -> None:
            nonlocal attempts
            attempts += 1
            raise HarnessTransportClosedError("Codex stdout closed", replay_safe=True)

        async def _prepare_turn_retry(self) -> bool:
            nonlocal cleanups
            cleanups += 1
            return True

    monkeypatch.setattr(_scaffold, "_TURN_IDLE_TIMEOUT_S", 10.0)
    monkeypatch.setattr(_scaffold, "_TURN_ABSOLUTE_TIMEOUT_S", 120.0)
    monkeypatch.setattr(_scaffold, "_WEDGED_TURN_RECOVERY_RETRIES", 1)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    ctx = TurnContext(response_id="exhausted", event_queue=queue, cancelled=asyncio.Event())
    async with asyncio.timeout(1):
        with pytest.raises(HarnessTransportClosedError):
            await App()._guarded_run_turn(None, ctx)  # type: ignore[arg-type]
    assert attempts == 2
    assert cleanups == 1
    assert isinstance(queue.get_nowait(), RetryEvent)
    assert queue.get_nowait() is None
    assert queue.empty()
