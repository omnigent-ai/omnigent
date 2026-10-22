"""Idle-monitor active-work accounting for background runner work.

Pins that ``app.state.has_active_work`` keeps the inactivity watchdog from
shutting down while ``sys_call_async`` tools, scheduled timers, or parked
approvals are still live — and that completion / cancel / failure release
the pin so a short idle timeout can shut down.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app, pending_approvals
from omnigent.runner._entry import _run_inactivity_monitor
from omnigent.runner.app import (
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


async def _native_app_with_session(conv_id: str, harness: str = "pi-native") -> FastAPI:
    """Build a runner app whose spec resolves to *harness* for *conv_id*.

    :param conv_id: Session id the caller will create, e.g. ``"conv_native"``.
    :param harness: Executor harness the spec declares, e.g. ``"pi-native"``.
    :returns: Fresh FastAPI runner app.
    """
    from omnigent.spec.types import AgentSpec, ExecutorSpec
    from tests.runner.conftest import (
        _FakeProcessManager,
        _ScriptedHarnessClient,
        _spec_resolver_returning,
        _sse,
    )

    spec = AgentSpec(
        spec_version=1,
        name="native-idle-test",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )
    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": f"resp_{conv_id}"}}),
            _sse({"type": "response.completed", "response": {"id": f"resp_{conv_id}"}}),
        ]
    )
    return create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=await _spec_resolver_returning(spec),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


async def _post_status(client: Any, conv_id: str, status: str) -> None:
    """POST one forwarder-style ``external_session_status`` edge.

    :param client: Runner test client.
    :param conv_id: Session id, e.g. ``"conv_native"``.
    :param status: Native status, e.g. ``"running"`` or ``"idle"``.
    :returns: None.
    """
    resp = await client.post(
        f"/v1/sessions/{conv_id}/events",
        json={"type": "external_session_status", "data": {"status": status}},
    )
    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "waiting"])
async def test_native_in_flight_status_blocks_idle_shutdown_until_idle(status: str) -> None:
    """A native terminal's in-flight status holds the watchdog until it settles.

    Native delivery leaves ``active_turns`` once the prompt is typed, so the
    terminal's ``running`` / ``waiting`` edge is the only sign of live work.

    :param status: In-flight native status to exercise.
    :returns: None.
    """
    from tests.runner.conftest import _runner_client

    conv_id = "conv_native_in_flight"
    app = await _native_app_with_session(conv_id)
    async with _runner_client(app) as client:
        created = await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag"})
        assert created.status_code == 201, created.text
        await _post_status(client, conv_id, status)
        assert conv_id not in app.state.active_turns
        assert app.state.has_active_work() is True

        async def _release() -> None:
            await _post_status(client, conv_id, "idle")

        await _assert_monitor_blocked_then_shuts_down(
            has_active_work=app.state.has_active_work,
            release=_release,
        )


@pytest.mark.asyncio
async def test_native_failed_status_releases_idle_pin() -> None:
    """A native ``failed`` edge settles the turn for the watchdog."""
    from tests.runner.conftest import _runner_client

    conv_id = "conv_native_failed"
    app = await _native_app_with_session(conv_id)
    async with _runner_client(app) as client:
        created = await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag"})
        assert created.status_code == 201, created.text
        await _post_status(client, conv_id, "running")
        assert app.state.has_active_work() is True
        await _post_status(client, conv_id, "failed")
        assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_native_pane_idle_after_mid_turn_follow_up_releases_pin() -> None:
    """A follow-up bound mid-turn does not strand the pin on the pane's one idle edge.

    The pane watcher dedups edges, so a prompt queued while the terminal is
    already running produces no fresh ``running`` — only the final ``idle``.
    """
    from tests.runner.conftest import _runner_client

    conv_id = "conv_native_follow_up"
    app = await _native_app_with_session(conv_id)
    publish_pane_status = app.state.session_resource_registry._session_status_publisher
    async with _runner_client(app) as client:
        created = await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag"})
        assert created.status_code == 201, created.text
        publish_pane_status(conv_id, "running", None)
        assert app.state.has_active_work() is True
        app.state.begin_turn_slot(conv_id)
        app.state.active_turns.pop(conv_id, None)
        publish_pane_status(conv_id, "idle", None)
        assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_required_native_terminal_exit_releases_idle_pin() -> None:
    """A required terminal that dies after delivery cannot leave the runner pinned."""
    from omnigent.runner.resource_registry import TerminalExitEvent, TerminalLifecycle
    from tests.runner.conftest import _runner_client

    conv_id = "conv_native_exit"
    app = await _native_app_with_session(conv_id)
    registry = app.state.session_resource_registry
    async with _runner_client(app) as client:
        created = await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag"})
        assert created.status_code == 201, created.text
        await _post_status(client, conv_id, "running")
        assert app.state.has_active_work() is True
        registry._terminal_exit_publisher(
            TerminalExitEvent(
                session_id=conv_id,
                terminal_id="terminal:pi:main",
                terminal_name="pi",
                session_key="main",
                lifecycle=TerminalLifecycle.REQUIRED,
                session_was_idle=True,
            )
        )
        assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_deleted_native_session_late_status_does_not_pin() -> None:
    """A status callback that lands after DELETE cannot keep the runner alive."""
    from tests.runner.conftest import _runner_client

    conv_id = "conv_native_deleted"
    app = await _native_app_with_session(conv_id)
    async with _runner_client(app) as client:
        created = await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag"})
        assert created.status_code == 201, created.text
        await _post_status(client, conv_id, "running")
        deleted = await client.delete(f"/v1/sessions/{conv_id}")
        assert deleted.status_code == 200, deleted.text
        assert app.state.has_active_work() is False
        await _post_status(client, conv_id, "running")
        assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_sdk_session_status_does_not_pin_idle_watchdog() -> None:
    """An SDK harness's published status is covered by ``active_turns`` alone."""
    from tests.runner.conftest import _runner_client

    conv_id = "conv_sdk_status"
    app = await _native_app_with_session(conv_id, harness="openai-agents")
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-model",
                "agent_id": "agent_idle_test",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        )
        assert resp.status_code == 202, resp.text
        for _ in range(100):
            if conv_id not in app.state.active_turns:
                break
            await asyncio.sleep(0.01)
        app.state.native_pane_status[conv_id] = "running"
        assert app.state.has_active_work() is False
