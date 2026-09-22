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
        fut.set_result(True)
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
    fut.set_result(False)
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
@pytest.mark.parametrize("status", ["running", "waiting"])
async def test_native_pane_status_blocks_idle_shutdown_until_idle(status: str) -> None:
    """A native pane's in-flight status remains active until it settles.

    Native terminal delivery can clear ``active_turns`` before the pane emits
    its terminal edge. The pane status must keep the idle watchdog blocked in
    both producing and user-input-waiting states, then release it on ``idle``.

    :param status: In-flight native status to exercise.
    :returns: None.
    """
    from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient

    conv_id = "conv_native"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        started = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "external_session_status", "data": {"status": status}},
        )
        assert started.status_code == 204, started.text
        assert app.state.has_active_work() is True, app.state.native_work_status

        async def _release() -> None:
            settled = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "external_session_status", "data": {"status": "idle"}},
            )
            assert settled.status_code == 204, settled.text

        await _assert_monitor_blocked_then_shuts_down(
            has_active_work=app.state.has_active_work,
            release=_release,
        )
        assert app.state.has_active_work() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_uncached_native_dispatch_pins_before_delivery_finishes(
    stream: bool,
) -> None:
    """A first native turn is pinned before its harness delivery can return.

    The first message can arrive before the runner's session-spec cache is
    warm. The resolved native harness must arm its pin before background or
    direct-stream delivery clears ``active_turns``.
    """
    from omnigent.spec.types import AgentSpec, ExecutorSpec
    from tests.runner.conftest import (
        _FakeProcessManager,
        _runner_client,
        _ScriptedHarnessClient,
        _spec_resolver_returning,
        _sse,
    )

    conv_id = "conv_uncached_native_pin"
    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_uncached"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_uncached"}}),
        ]
    )
    spec = AgentSpec(
        spec_version=1,
        name="native-idle-test",
        executor=ExecutorSpec(type="omnigent", config={"harness": "goose-native"}),
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=await _spec_resolver_returning(spec),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        response = await client.post(
            f"/v1/sessions/{conv_id}/events?stream={'true' if stream else 'false'}",
            json={
                "type": "message",
                "role": "user",
                "model": "codex",
                "agent_id": "agent_native",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        )
        assert response.status_code == (200 if stream else 202), response.text
        for _ in range(100):
            if conv_id not in app.state.active_turns:
                break
            await asyncio.sleep(0.01)
        assert conv_id not in app.state.active_turns
        assert app.state.has_active_work() is True, app.state.native_work_status

        settled = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle"},
            },
        )
        assert settled.status_code == 204, settled.text
        assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_switch_to_non_native_harness_releases_native_pin() -> None:
    """A native-to-SDK harness switch does not retain native liveness."""
    from tests.runner.conftest import (
        _FakeProcessManager,
        _runner_client,
        _ScriptedHarnessClient,
        _sse,
    )

    conv_id = "conv_native_to_sdk"
    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_sdk"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_sdk"}}),
        ]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        started = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "external_session_status", "data": {"status": "running"}},
        )
        assert started.status_code == 204, started.text
        assert app.state.has_active_work() is True

        response = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "sdk",
                "content": [{"type": "input_text", "text": "hello"}],
                "harness_override": "openai-agents",
            },
        )
        assert response.status_code == 202, response.text
        for _ in range(100):
            if conv_id not in app.state.active_turns:
                break
            await asyncio.sleep(0.01)
        assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_native_failed_external_status_releases_pin() -> None:
    """A matching native failure settles the watchdog pin."""
    from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient

    conv_id = "conv_native_failed_pin"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        started = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "running", "response_id": "resp_failed"},
            },
        )
        assert started.status_code == 204, started.text
        assert app.state.has_active_work() is True
        failed = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "failed", "response_id": "resp_failed"},
            },
        )
        assert failed.status_code == 204, failed.text
        assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_deleted_native_session_ignores_late_status_callback() -> None:
    """A callback queued across DELETE cannot recreate a native pin."""
    from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient

    conv_id = "conv_native_deleted_pin"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        started = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "running", "response_id": "resp_deleted"},
            },
        )
        assert started.status_code == 204, started.text
        assert app.state.has_active_work() is True
        deleted = await client.delete(f"/v1/sessions/{conv_id}")
        assert deleted.status_code == 200, deleted.text
        assert app.state.has_active_work() is False
        late = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "running", "response_id": "resp_deleted"},
            },
        )
        assert late.status_code == 204, late.text
        assert app.state.has_active_work() is False
        assert conv_id not in app.state.native_pane_status


@pytest.mark.asyncio
async def test_old_native_terminal_status_does_not_clear_new_turn_pin() -> None:
    """A delayed old response terminal edge cannot settle a new bind epoch."""
    from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient

    conv_id = "conv_native_generation_pin"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        first = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "running", "response_id": "resp_old"},
            },
        )
        assert first.status_code == 204, first.text
        app.state.begin_turn_slot(conv_id)
        app.state.active_turns.pop(conv_id, None)

        stale = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "response_id": "resp_old"},
            },
        )
        assert stale.status_code == 204, stale.text
        assert app.state.has_active_work() is True

        current = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "running", "response_id": "resp_new"},
            },
        )
        assert current.status_code == 204, current.text
        settled = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "response_id": "resp_new"},
            },
        )
        assert settled.status_code == 204, settled.text
        assert app.state.has_active_work() is False
