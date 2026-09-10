"""Idle-monitor active-work accounting for background runner work.

Pins that ``app.state.has_active_work`` keeps the inactivity watchdog from
shutting down while ``sys_call_async`` tools, scheduled timers, or parked
approvals are still live — and that completion / cancel / failure release
the pin so a short idle timeout can shut down.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app, pending_approvals
from omnigent.runner._entry import _run_inactivity_monitor
from omnigent.runner.app import (
    _NATIVE_PANE_TURN_STALE_S,
    _has_fresh_native_pane_turn,
    _has_live_async_tasks,
    _session_timers,
    register_timer,
    unregister_timer,
)
from tests.runner.helpers import NullServerClient


@pytest.fixture(autouse=True)
def _clean_global_active_work_state() -> None:
    """
    Reset module-global timer / approval registries between tests.

    :returns: None.
    """
    pending_approvals.reset_for_tests()
    for session_timers in list(_session_timers.values()):
        for task in list(session_timers.values()):
            task.cancel()
    _session_timers.clear()
    yield
    pending_approvals.reset_for_tests()
    for session_timers in list(_session_timers.values()):
        for task in list(session_timers.values()):
            task.cancel()
    _session_timers.clear()


def _scaffold_app() -> FastAPI:
    """
    Build a scaffold runner app (no harness process manager).

    :returns: Fresh FastAPI runner app.
    """
    return create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]


def _register_async_handle(
    registry: dict[str, dict[str, tuple[asyncio.Task[str], asyncio.Event]]],
    *,
    session_id: str,
    handle_id: str,
    task: asyncio.Task[str],
) -> None:
    """
    Insert a live ``sys_call_async`` registry entry for idle-monitor tests.

    :param registry: Async-tool registry to mutate.
    :param session_id: Session key, e.g. ``"conv_async"``.
    :param handle_id: Async handle id, e.g. ``"handle_test"``.
    :param task: Background task standing in for the async tool.
    :returns: None.
    """
    registry.setdefault(session_id, {})[handle_id] = (task, asyncio.Event())


async def _assert_monitor_blocked_then_shuts_down(
    *,
    has_active_work: Any,
    release: Any,
) -> None:
    """
    Prove a short idle timeout waits for active work, then shuts down.

    :param has_active_work: Callback matching ``app.state.has_active_work``.
    :param release: Awaitable that clears the active-work pin.
    :returns: None.
    """
    loop = asyncio.get_running_loop()
    shutdowns: list[str] = []
    monitor = asyncio.create_task(
        _run_inactivity_monitor(
            idle_timeout_s=0.01,
            get_last_activity=lambda: loop.time() - 1.0,
            has_active_work=has_active_work,
            request_shutdown=lambda: shutdowns.append("shutdown"),
            poll_interval_s=0.005,
        )
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(monitor), timeout=0.03)
    assert shutdowns == []
    assert not monitor.done()

    await release()
    await asyncio.wait_for(monitor, timeout=0.2)
    assert shutdowns == ["shutdown"]


@pytest.mark.asyncio
async def test_running_async_tool_blocks_idle_shutdown() -> None:
    """A live ``sys_call_async`` task prevents idle shutdown.

    :returns: None.
    """
    registry: dict[str, dict[str, tuple[asyncio.Task[str], asyncio.Event]]] = {}
    started = asyncio.Event()
    finish = asyncio.Event()

    async def _bg() -> str:
        started.set()
        await finish.wait()
        return "ok"

    task = asyncio.create_task(_bg(), name="async-handle_live")
    _register_async_handle(registry, session_id="conv_async", handle_id="handle_live", task=task)
    await started.wait()
    assert _has_live_async_tasks(registry) is True

    async def _release() -> None:
        finish.set()
        await task
        registry["conv_async"].pop("handle_live", None)

    await _assert_monitor_blocked_then_shuts_down(
        has_active_work=lambda: _has_live_async_tasks(registry),
        release=_release,
    )
    assert _has_live_async_tasks(registry) is False


@pytest.mark.asyncio
async def test_completed_async_tool_is_idle_eligible() -> None:
    """After an async tool finishes, the runner is idle-eligible.

    :returns: None.
    """
    registry: dict[str, dict[str, tuple[asyncio.Task[str], asyncio.Event]]] = {}

    async def _bg() -> str:
        return "done"

    task = asyncio.create_task(_bg(), name="async-handle_done")
    _register_async_handle(registry, session_id="conv_async", handle_id="handle_done", task=task)
    await task
    # Stale registry entry must not count once the task is done.
    assert _has_live_async_tasks(registry) is False

    loop = asyncio.get_running_loop()
    shutdowns: list[str] = []
    await asyncio.wait_for(
        _run_inactivity_monitor(
            idle_timeout_s=0.01,
            get_last_activity=lambda: loop.time() - 1.0,
            has_active_work=lambda: _has_live_async_tasks(registry),
            request_shutdown=lambda: shutdowns.append("shutdown"),
            poll_interval_s=0.001,
        ),
        timeout=0.2,
    )
    assert shutdowns == ["shutdown"]


@pytest.mark.asyncio
async def test_cancelled_async_tool_releases_active_work() -> None:
    """Cancellation clears active-work status for idle shutdown.

    :returns: None.
    """
    registry: dict[str, dict[str, tuple[asyncio.Task[str], asyncio.Event]]] = {}
    gate = asyncio.Event()

    async def _bg() -> str:
        await gate.wait()
        return "never"

    task = asyncio.create_task(_bg(), name="async-handle_cancel")
    _register_async_handle(registry, session_id="conv_async", handle_id="handle_cancel", task=task)
    assert _has_live_async_tasks(registry) is True

    async def _release() -> None:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        registry["conv_async"].pop("handle_cancel", None)

    await _assert_monitor_blocked_then_shuts_down(
        has_active_work=lambda: _has_live_async_tasks(registry),
        release=_release,
    )
    assert _has_live_async_tasks(registry) is False


@pytest.mark.asyncio
async def test_failed_async_tool_releases_active_work() -> None:
    """A failed async tool releases active-work status.

    :returns: None.
    """
    registry: dict[str, dict[str, tuple[asyncio.Task[str], asyncio.Event]]] = {}
    gate = asyncio.Event()

    async def _bg() -> str:
        await gate.wait()
        raise RuntimeError("async tool boom")

    task = asyncio.create_task(_bg(), name="async-handle_fail")
    _register_async_handle(registry, session_id="conv_async", handle_id="handle_fail", task=task)
    assert _has_live_async_tasks(registry) is True

    async def _release() -> None:
        gate.set()
        with pytest.raises(RuntimeError, match="async tool boom"):
            await task
        # Leave the stale entry; done() must still make the runner idle-eligible.
        assert "handle_fail" in registry["conv_async"]

    await _assert_monitor_blocked_then_shuts_down(
        has_active_work=lambda: _has_live_async_tasks(registry),
        release=_release,
    )
    assert _has_live_async_tasks(registry) is False


@pytest.mark.asyncio
async def test_live_timer_blocks_idle_shutdown() -> None:
    """A registered timer task pins the runner until it completes.

    :returns: None.
    """
    app = _scaffold_app()
    finish = asyncio.Event()

    async def _timer() -> None:
        await finish.wait()

    task = asyncio.create_task(_timer(), name="timer-pin")
    register_timer("conv_timer", "timer_pin", task)
    assert app.state.has_active_work() is True

    async def _release() -> None:
        finish.set()
        await task
        unregister_timer("conv_timer", "timer_pin")

    await _assert_monitor_blocked_then_shuts_down(
        has_active_work=app.state.has_active_work,
        release=_release,
    )
    assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_parked_approval_blocks_idle_shutdown() -> None:
    """A parked ASK Future keeps the runner alive until resolved.

    :returns: None.
    """
    app = _scaffold_app()
    fut = pending_approvals.register("elicit_idle_pin")
    assert app.state.has_active_work() is True

    async def _release() -> None:
        fut.set_result(pending_approvals.Verdict(approved=True))
        pending_approvals.cleanup("elicit_idle_pin")

    await _assert_monitor_blocked_then_shuts_down(
        has_active_work=app.state.has_active_work,
        release=_release,
    )
    assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_done_approval_future_does_not_pin_runner() -> None:
    """A completed approval Future left in the registry is not active work.

    :returns: None.
    """
    app = _scaffold_app()
    fut = pending_approvals.register("elicit_stale")
    fut.set_result(pending_approvals.Verdict(approved=False))
    assert fut.done()
    assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_drain_session_streams_enqueues_done_sentinel() -> None:
    """Graceful shutdown signals end-of-stream to every open session stream.

    ``app.state.drain_session_streams`` puts the ``None`` sentinel on each
    session event queue so its ``GET /stream`` generator emits ``[DONE]`` and
    the server relay returns cleanly — the mechanism that turns an idle-reaped
    runner's abrupt drop into a quiet end-of-stream (no scary error banner).
    """
    from omnigent.runner.app import _session_event_queues_ref

    app = _scaffold_app()
    q_a: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    q_b: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    _session_event_queues_ref["conv_drain_a"] = q_a
    _session_event_queues_ref["conv_drain_b"] = q_b
    try:
        app.state.drain_session_streams()
        # Each open stream received exactly the end-of-stream sentinel.
        assert q_a.get_nowait() is None
        assert q_b.get_nowait() is None
        assert q_a.empty()
        assert q_b.empty()
    finally:
        _session_event_queues_ref.pop("conv_drain_a", None)
        _session_event_queues_ref.pop("conv_drain_b", None)


@pytest.mark.asyncio
async def test_native_pane_running_blocks_idle_shutdown() -> None:
    """A running native terminal turn keeps the runner alive until it settles.

    Native turns leave ``_active_turns`` once terminal delivery takes over, so
    the watchdog must count the pane's ``running`` status instead.

    :returns: None.
    """
    from omnigent.runner.app import _session_event_queues_ref

    app = _scaffold_app()
    publish_status = app.state.session_resource_registry._session_status_publisher
    assert callable(publish_status)
    try:
        publish_status("conv_native_running", "running")
        assert app.state.has_active_work() is True

        async def _release() -> None:
            publish_status("conv_native_running", "idle")

        await _assert_monitor_blocked_then_shuts_down(
            has_active_work=app.state.has_active_work,
            release=_release,
        )
        assert app.state.has_active_work() is False
    finally:
        _session_event_queues_ref.pop("conv_native_running", None)


@pytest.mark.asyncio
async def test_native_pane_waiting_blocks_idle_shutdown() -> None:
    """A native pane blocked on user elicitation input is active work.

    ``waiting`` is a mid-turn state (the agent asked the user something); a
    shutdown there loses the user's answer, like reaping a parked approval.

    :returns: None.
    """
    from omnigent.runner.app import _session_event_queues_ref

    app = _scaffold_app()
    publish_status = app.state.session_resource_registry._session_status_publisher
    assert callable(publish_status)
    try:
        publish_status("conv_native_waiting", "waiting")
        assert app.state.has_active_work() is True

        async def _release() -> None:
            publish_status("conv_native_waiting", "idle")

        await _assert_monitor_blocked_then_shuts_down(
            has_active_work=app.state.has_active_work,
            release=_release,
        )
        assert app.state.has_active_work() is False
    finally:
        _session_event_queues_ref.pop("conv_native_waiting", None)


def test_native_pane_turn_pin_expires_without_activity() -> None:
    """A stale native status cannot keep an abandoned runner alive forever.

    :returns: None.
    """
    activity_at = {"conv_native": 100.0}
    for status in ("running", "waiting"):
        statuses = {"conv_native": status}
        assert _has_fresh_native_pane_turn(statuses, activity_at, now=100.0) is True
        assert (
            _has_fresh_native_pane_turn(
                statuses,
                activity_at,
                now=100.0 + _NATIVE_PANE_TURN_STALE_S + 1.0,
            )
            is False
        )
    # A settled pane never pins, and a status without a stamp never pins.
    assert _has_fresh_native_pane_turn({"conv_native": "idle"}, activity_at, now=100.0) is False
    assert _has_fresh_native_pane_turn({"conv_native": "running"}, {}, now=100.0) is False


def test_native_terminal_activity_refreshes_turn_pin_and_idle_timer() -> None:
    """Terminal output refreshes the native turn pin and the runner idle clock.

    Native status edges are sparse (one ``running`` at turn start), so a long
    producing turn must stay pinned via ``session.terminal.activity`` pulses;
    a settled pane's output must not re-pin. Each native status edge also
    resets the runner-level activity clock, so a settling turn restarts the
    idle window instead of being reaped the instant it finishes.

    :returns: None.
    """
    from omnigent.runner.app import _session_event_queues_ref

    app = _scaffold_app()
    registry = app.state.session_resource_registry
    publish_status = registry._session_status_publisher
    publish_activity = registry._terminal_activity_publisher
    assert callable(publish_status)
    assert callable(publish_activity)
    activity_marks: list[str] = []
    app.state.mark_activity = lambda: activity_marks.append("activity")
    try:
        for status in ("running", "waiting"):
            publish_status("conv_native_refresh", status)
            # The pane has produced nothing for longer than the staleness bound.
            app.state.native_pane_activity_at["conv_native_refresh"] = (
                time.monotonic() - _NATIVE_PANE_TURN_STALE_S - 1.0
            )
            assert app.state.has_active_work() is False

            publish_activity("conv_native_refresh", "terminal_codex_main")
            assert app.state.has_active_work() is True

        publish_status("conv_native_refresh", "idle")
        assert app.state.has_active_work() is False
        # Output from a settled pane must not resurrect the pin.
        publish_activity("conv_native_refresh", "terminal_codex_main")
        assert app.state.has_active_work() is False
        # Each of the 3 native status edges reset the runner idle clock.
        assert activity_marks == ["activity"] * 3
    finally:
        _session_event_queues_ref.pop("conv_native_refresh", None)


def test_inprocess_turn_status_does_not_pin_idle_watchdog() -> None:
    """A ``running`` edge from an in-process turn never creates a pane pin.

    In-process turns are counted via ``_active_turns`` and publish their
    status edges through ``_publish_event`` without the native channel's
    stamp; they may settle without a trailing ``idle`` status event, so
    counting them here would pin the runner after the turn ended. A side
    terminal's output must not conjure a pin for them either.

    :returns: None.
    """
    from omnigent.runner.app import _session_event_queues_ref

    app = _scaffold_app()
    publish_activity = app.state.session_resource_registry._terminal_activity_publisher
    assert callable(publish_activity)
    try:
        # The recorded-but-unstamped state an in-process turn's status edge
        # leaves behind in ``_publish_event``.
        app.state.native_pane_status["conv_inprocess"] = "running"
        assert app.state.has_active_work() is False

        # A side shell producing output must not stamp (and so pin) it.
        publish_activity("conv_inprocess", "terminal_side_shell")
        assert app.state.has_active_work() is False
    finally:
        _session_event_queues_ref.pop("conv_inprocess", None)
