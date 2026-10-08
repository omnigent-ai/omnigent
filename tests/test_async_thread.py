"""Worker failures must fail the caller even when its event loop is running."""

import asyncio
import contextvars
import threading

import pytest

from tests._helpers.async_thread import run_in_fresh_loop


@pytest.mark.asyncio
async def test_sync_cleanup_keeps_loop_live_and_finishes_before_repeat_cancel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Owned blocking cleanup survives cancellation without freezing the loop."""
    from omnigent.inner.async_utils import run_sync_cleanup

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    cleanup_scope = contextvars.ContextVar("cleanup_scope", default=None)
    observed_scope: list[str | None] = []
    heartbeat_ran_while_blocked = asyncio.Event()

    def blocking_cleanup() -> None:
        observed_scope.append(cleanup_scope.get())
        started.set()
        assert release.wait(timeout=2.0)
        finished.set()

    async def heartbeat() -> None:
        while not finished.is_set():
            if started.is_set():
                heartbeat_ran_while_blocked.set()
                return
            await asyncio.sleep(0.01)

    token = cleanup_scope.set("session-cleanup")
    heartbeat_task: asyncio.Task[None] | None = None
    cleanup_task: asyncio.Task[object] | None = None
    watchdog = threading.Thread(
        target=lambda: (release.wait(timeout=2.0), release.set()),
        name="test-cleanup-watchdog",
        daemon=True,
    )
    watchdog.start()
    try:
        heartbeat_task = asyncio.create_task(heartbeat())
        cleanup_task = asyncio.create_task(
            run_sync_cleanup(
                blocking_cleanup,
                component="test_cleanup",
                session_id="session-cleanup",
                slow_threshold_s=0.0,
            )
        )
        await asyncio.wait_for(heartbeat_ran_while_blocked.wait(), timeout=1.0)
        cleanup_task.cancel()
        await asyncio.sleep(0)
        cleanup_task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup_task
    finally:
        release.set()
        if cleanup_task is not None and not cleanup_task.done():
            cleanup_task.cancel()
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
        await asyncio.gather(cleanup_task, heartbeat_task, return_exceptions=True)
        watchdog.join(timeout=1.0)
        cleanup_scope.reset(token)

    assert observed_scope == ["session-cleanup"]
    assert heartbeat_ran_while_blocked.is_set()
    slow = [
        record
        for record in caplog.records
        if record.getMessage() == "Synchronous cleanup completed slowly"
    ]
    assert slow
    assert slow[-1].event_name == "sync_cleanup_slow"
    assert slow[-1].session_id == "session-cleanup"
    assert slow[-1].attributes["component"] == "test_cleanup"


@pytest.mark.asyncio
async def test_sync_cleanup_cancellation_wins_over_worker_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cleanup error is logged but cannot replace caller cancellation."""
    from omnigent.inner.async_utils import run_sync_cleanup

    started = threading.Event()
    release = threading.Event()

    def failing_cleanup() -> None:
        started.set()
        assert release.wait(timeout=2.0)
        raise ValueError("cleanup failed")

    cleanup_task = asyncio.create_task(
        run_sync_cleanup(
            failing_cleanup,
            component="failing_cleanup",
            session_id="session-failing-cleanup",
            slow_threshold_s=0.0,
        )
    )
    assert await asyncio.to_thread(started.wait, 1.0)
    cleanup_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup_task

    failure = next(
        record
        for record in caplog.records
        if record.getMessage() == "Synchronous cleanup failed after cancellation"
    )
    assert failure.event_name == "sync_cleanup_failed"
    assert failure.session_id == "session-failing-cleanup"
    assert failure.attributes["component"] == "failing_cleanup"
    assert failure.attributes["error_type"] == "ValueError"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("worker failed"), asyncio.CancelledError()])
async def test_worker_failure_reaches_caller(error: BaseException) -> None:
    caller_loop = asyncio.get_running_loop()

    async def fail() -> None:
        assert asyncio.get_running_loop() is not caller_loop
        raise error

    with pytest.raises(type(error)) as caught:
        run_in_fresh_loop(fail())
    assert caught.value is error
