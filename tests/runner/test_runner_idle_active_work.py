"""Idle-monitor active-work accounting for background runner work.

Pins that ``app.state.has_active_work`` keeps the inactivity watchdog from
shutting down while ``sys_call_async`` tools, scheduled timers, or parked
approvals are still live — and that completion / cancel / failure release
the pin so a short idle timeout can shut down.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
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
from omnigent.terminals.registry import TerminalRegistry
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


def _native_spec(harness: str) -> Any:
    """An agent spec whose executor runs *harness*, e.g. ``"pi-native"``."""
    from omnigent.spec.types import AgentSpec, ExecutorSpec

    return AgentSpec(
        spec_version=1,
        name="native-idle-test",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )


async def _native_app_with_session(
    conv_id: str,
    harness: str = "pi-native",
    *,
    status_clock: Callable[[], float] | None = None,
    terminal_registry: TerminalRegistry | None = None,
) -> FastAPI:
    """Build a runner app whose spec resolves to *harness* for *conv_id*.

    :param conv_id: Session id the caller will create, e.g. ``"conv_native"``.
    :param harness: Executor harness the spec declares, e.g. ``"pi-native"``.
    :param status_clock: Monotonic clock for the status book, e.g. a fake.
    :param terminal_registry: Terminal registry the resource registry observes.
    :returns: Fresh FastAPI runner app.
    """
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from tests.runner.conftest import (
        _FakeProcessManager,
        _ScriptedHarnessClient,
        _spec_resolver_returning,
        _sse,
    )

    spec = _native_spec(harness)
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
        resource_registry=SessionResourceRegistry(
            terminal_registry=terminal_registry, status_clock=status_clock
        ),
    )


async def _post_status(
    client: Any, conv_id: str, status: str, *, blocked_on: str | None = None
) -> None:
    """POST one forwarder-style ``external_session_status`` edge.

    :param client: Runner test client.
    :param conv_id: Session id, e.g. ``"conv_native"``.
    :param status: Native status, e.g. ``"running"`` or ``"idle"``.
    :param blocked_on: Dialog reason the forwarder attaches, if any.
    :returns: None.
    """
    data: dict[str, str] = {"status": status}
    if blocked_on is not None:
        data["blocked_on"] = blocked_on
    resp = await client.post(
        f"/v1/sessions/{conv_id}/events",
        json={"type": "external_session_status", "data": data},
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
async def test_native_pane_idle_after_mid_turn_follow_up_releases_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A follow-up bound mid-turn does not strand the pin on the pane's one idle edge.

    The pane watcher dedups edges, so a prompt queued while the terminal is
    already running produces no fresh ``running`` — only the final ``idle``.
    The edges come from the real watcher closure, which records them in the
    status book before the wire dedup.
    """
    from tests.runner.conftest import _runner_client, _spec_resolver_returning
    from tests.terminals.native_pane_rig import build_pane_rig

    rig = await build_pane_rig(
        tmp_path,
        monkeypatch,
        key="pi",
        spec_resolver=await _spec_resolver_returning(_native_spec("pi-native")),
    )
    app, conv_id = rig.app, rig.conv_id
    try:
        async with _runner_client(app) as client:
            created = await client.post(
                "/v1/sessions", json={"session_id": conv_id, "agent_id": "ag"}
            )
            assert created.status_code == 201, created.text
            await rig.fire("on_activity")
            assert app.state.has_active_work() is True
            app.state.begin_turn_slot(conv_id)
            app.state.active_turns.pop(conv_id, None)
            await rig.fire("on_idle")
            assert app.state.has_active_work() is False
    finally:
        rig.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "role"),
    [("pi-native", "pi-native"), ("pi-native", None), ("codex-native", "codex-native")],
    ids=["native_pane", "role_less_pane", "codex_pane_with_app_server"],
)
async def test_required_native_terminal_exit_releases_idle_pin(
    tmp_path: Path, harness: str, role: str | None
) -> None:
    """A required terminal that dies after delivery cannot leave the runner pinned.

    The exit arrives as the pane watcher reports it, through the registry.
    The registry resets a native pane's session, and the runner's exit
    publisher resets any required terminal's. A codex app-server does not
    keep the turn: a required terminal's death ends the session.
    """
    from omnigent.runner.native import orchestration
    from omnigent.runner.resource_registry import TerminalLifecycle
    from tests.runner.conftest import _runner_client
    from tests.runner.helpers import make_test_terminal_instance
    from tests.terminals.native_pane_rig import _FakeServerProcess

    conv_id = "conv_native_exit"
    name = harness.removesuffix("-native")
    terminals = TerminalRegistry()
    app = await _native_app_with_session(conv_id, harness=harness, terminal_registry=terminals)
    registry = app.state.session_resource_registry
    instance = make_test_terminal_instance(name, "main", tmp_path)

    async def _close() -> None:
        instance.running = False

    async def _no_link(_link: str) -> None:
        return None

    instance.close = _close  # type: ignore[method-assign]
    instance.set_conversation_link = _no_link  # type: ignore[method-assign]
    instance.start_idle_watcher_thread = lambda *_a, **_k: None  # type: ignore[method-assign]
    terminals._by_conversation.setdefault(conv_id, {})[(name, "main")] = instance
    if harness == "codex-native":
        orchestration._AUTO_CODEX_APP_SERVERS[conv_id] = _FakeServerProcess()  # type: ignore[assignment]
    try:
        async with _runner_client(app) as client:
            created = await client.post(
                "/v1/sessions", json={"session_id": conv_id, "agent_id": "ag"}
            )
            assert created.status_code == 201, created.text
            await registry.observe_required_terminal(
                conv_id, name, "main", instance, resource_role=role
            )
            await _post_status(client, conv_id, "running")
            assert app.state.has_active_work() is True
            # The pane last reported its prompt back, so this is a clean exit.
            registry._set_session_status_memo(conv_id, "idle")
            await registry._handle_terminal_exit(
                session_id=conv_id,
                terminal_name=name,
                session_key="main",
                lifecycle=TerminalLifecycle.REQUIRED,
                instance=instance,
            )
            assert app.state.has_active_work() is False
    finally:
        orchestration._AUTO_CODEX_APP_SERVERS.pop(conv_id, None)


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
    """An SDK harness's recorded status is covered by ``active_turns`` alone."""
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
        await _post_status(client, conv_id, "running")
        record = app.state.session_status_book.current(conv_id)
        assert record is not None and record.status == "running"
        assert app.state.has_active_work() is False


# ── a native turn holds the runner only while it shows evidence of work ──

_CEILING_S = 7200.0
_MAX_TURN_ENV = "OMNIGENT_NATIVE_PANE_MAX_TURN_S"
_APPROVAL_MAX_ENV = "OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S"


class _Clock:
    """A hand-driven monotonic clock, e.g. for the status book."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event_name", None) == name]


async def _create_session(client: Any, conv_id: str) -> None:
    created = await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag"})
    assert created.status_code == 201, created.text


async def _dispatch_turn(client: Any, app: FastAPI, conv_id: str) -> None:
    """Send a real user message and wait until the runner's side of the turn ends."""
    resp = await client.post(
        f"/v1/sessions/{conv_id}/events",
        json={
            "type": "message",
            "agent_id": "ag",
            "content": [{"type": "input_text", "text": "go on"}],
        },
    )
    assert resp.status_code == 202, resp.text
    for _ in range(200):
        if conv_id not in app.state.active_turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the runner's turn never ended")


def _agent_output(instance: Any, *, ago_s: float = 0.0) -> None:
    """Stamp pane output on *instance* as its idle watcher does, *ago_s* ago."""
    instance._last_agent_output_at = time.monotonic() - ago_s


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses", [("running",), ("running", "waiting")], ids=["running", "waiting_after_running"]
)
async def test_a_lost_closing_edge_holds_the_runner_only_until_the_ceiling(
    statuses: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recorded in-flight status whose closing edge is lost cannot pin the runner.

    The forwarder's ``idle`` never arrives, so the book keeps the last
    in-flight status. With no dispatch or pane output since its episode
    began, it holds the watchdog until the ceiling, then the runner shuts
    down. Each expired episode logs one WARNING.

    :param statuses: Relayed edges; the last one is never closed.
    """
    from tests.runner.conftest import _runner_client

    monkeypatch.setenv(_MAX_TURN_ENV, str(_CEILING_S))
    clock = _Clock()
    conv_id = "conv_native_lost_idle"
    app = await _native_app_with_session(conv_id, harness="codex-native", status_clock=clock)
    async with _runner_client(app) as client:
        await _create_session(client, conv_id)
        for status in statuses:
            clock.now += 60.0
            await _post_status(client, conv_id, status)
        clock.now += _CEILING_S - 1.0
        assert app.state.has_active_work() is True

        async def _ceiling_passes() -> None:
            clock.now += 1.0

        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            await _assert_monitor_blocked_then_shuts_down(
                has_active_work=app.state.has_active_work,
                release=_ceiling_passes,
            )
            assert app.state.has_active_work() is False
            assert len(_events(caplog, "runner_in_flight_hold_expired")) == 1
            # A settled turn and a new one open a new episode, warned once more.
            await _post_status(client, conv_id, "idle")
            await _post_status(client, conv_id, statuses[-1])
            assert app.state.has_active_work() is True
            clock.now += _CEILING_S
            assert app.state.has_active_work() is False
            assert app.state.has_active_work() is False
        expired = _events(caplog, "runner_in_flight_hold_expired")
        assert len(expired) == 2, [r.getMessage() for r in expired]
        assert expired[0].session_id == conv_id  # type: ignore[attr-defined]
        assert expired[0].attributes == {  # type: ignore[attr-defined]
            "status": statuses[-1],
            "claim_source": "relay",
            "evidence_age_s": _CEILING_S,
            "ceiling_s": _CEILING_S,
        }


@pytest.mark.asyncio
async def test_a_reposted_running_cannot_extend_the_runner_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relay re-posting ``running`` is not evidence of work.

    Duplicates keep the episode's start, so re-posts cannot move the end of
    the hold. A settled turn followed by a new ``running``, or a new runner
    dispatch, restarts it.
    """
    from tests.runner.conftest import _runner_client

    monkeypatch.setenv(_MAX_TURN_ENV, str(_CEILING_S))
    clock = _Clock()
    conv_id = "conv_native_reposted"
    app = await _native_app_with_session(conv_id, harness="codex-native", status_clock=clock)
    async with _runner_client(app) as client:
        await _create_session(client, conv_id)
        await _post_status(client, conv_id, "running")
        for _ in range(5):
            clock.now += _CEILING_S / 5
            await _post_status(client, conv_id, "running")
        assert app.state.has_active_work() is False

        await _post_status(client, conv_id, "idle")
        await _post_status(client, conv_id, "running")
        assert app.state.has_active_work() is True
        clock.now += _CEILING_S
        assert app.state.has_active_work() is False

        # The running is still never closed; the next dispatch restarts the clock.
        await _dispatch_turn(client, app, conv_id)
        clock.now += _CEILING_S - 1.0
        assert app.state.has_active_work() is True
        clock.now += 1.0
        assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_a_printing_native_turn_holds_the_runner_past_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn whose agent pane keeps printing is never cut off by its age.

    Output on the session's own agent pane renews the hold, so a codex turn
    that runs for several ceilings keeps the runner up until its relayed
    ``idle``. A silent pane is held until the ceiling after its last output,
    and a side shell's output is not the agent's work.
    """
    from tests.runner.conftest import _runner_client, _spec_resolver_returning
    from tests.runner.helpers import make_test_terminal_instance
    from tests.terminals.native_pane_rig import build_pane_rig

    monkeypatch.setenv(_MAX_TURN_ENV, str(_CEILING_S))
    clock = _Clock()
    rig = await build_pane_rig(
        tmp_path,
        monkeypatch,
        key="codex",
        status_clock=clock,
        spec_resolver=await _spec_resolver_returning(_native_spec("codex-native")),
    )
    app, conv_id = rig.app, rig.conv_id
    try:
        async with _runner_client(app) as client:
            await _create_session(client, conv_id)
            agent_pane = rig.terminal_registry.get(conv_id, "codex", "main")
            side_shell = make_test_terminal_instance("bash", "side", tmp_path)
            rig.terminal_registry._by_conversation[conv_id][("bash", "side")] = side_shell
            await _post_status(client, conv_id, "running")
            for _ in range(6):
                clock.now += _CEILING_S / 2
                _agent_output(agent_pane)
                assert app.state.has_active_work() is True

            _agent_output(agent_pane, ago_s=_CEILING_S - 1.0)
            assert app.state.has_active_work() is True
            _agent_output(agent_pane, ago_s=_CEILING_S)
            _agent_output(side_shell)
            assert app.state.has_active_work() is False

            _agent_output(agent_pane)

            async def _relayed_idle() -> None:
                await _post_status(client, conv_id, "idle")

            await _assert_monitor_blocked_then_shuts_down(
                has_active_work=app.state.has_active_work,
                release=_relayed_idle,
            )
    finally:
        rig.drain()


@pytest.mark.asyncio
async def test_a_six_hour_native_turn_holds_the_runner_until_its_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the default ceiling, a turn many runner idle windows long is held.

    Nothing refreshes the evidence for six hours: no output, no dispatch.
    The runner still waits for the turn's own relayed ``idle``.
    """
    from tests.runner.conftest import _runner_client

    monkeypatch.delenv(_MAX_TURN_ENV, raising=False)
    clock = _Clock()
    conv_id = "conv_native_long_turn"
    app = await _native_app_with_session(conv_id, harness="codex-native", status_clock=clock)
    async with _runner_client(app) as client:
        await _create_session(client, conv_id)
        await _post_status(client, conv_id, "running")
        clock.now += 6 * 3600.0
        assert app.state.has_active_work() is True

        async def _relayed_idle() -> None:
            await _post_status(client, conv_id, "idle")

        await _assert_monitor_blocked_then_shuts_down(
            has_active_work=app.state.has_active_work,
            release=_relayed_idle,
        )


@pytest.mark.asyncio
async def test_a_claude_exit_banner_frees_the_runner_and_records_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Claude ``/exit`` just after its turn frees the runner and leaves ``idle``.

    The edges and the exit come from the real pane watcher closure, through
    the registry. Printing the resume banner after the turn's ``idle`` is pane
    activity, so the pane reads ``running`` again and the exit is not idle.
    The registry resets the claude pane's session, then the exit publisher
    records the clean stop's ``idle`` as a runner edge: no stale ``running``
    holds the runner and no ``failed`` is left.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.runner.session_status import StatusSource
    from tests.runner.conftest import _runner_client
    from tests.runner.helpers import make_test_terminal_instance

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    conv_id = "conv_claude_exit_banner"
    terminals = TerminalRegistry()
    app = await _native_app_with_session(
        conv_id, harness="claude-native", terminal_registry=terminals
    )
    registry = app.state.session_resource_registry
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    callbacks: dict[str, Callable[[], None]] = {}

    def _capture_watcher(on_idle: Callable[[], None] | None = None, **kwargs: Any) -> None:
        for name, callback in (("on_idle", on_idle), *kwargs.items()):
            if callable(callback):
                callbacks[name] = callback

    async def _close() -> None:
        instance.running = False

    async def _fire(name: str) -> None:
        await asyncio.to_thread(callbacks[name])
        for _ in range(3):
            await asyncio.sleep(0)

    instance.close = _close  # type: ignore[method-assign]
    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    instance.pane_pid_sync = lambda: 4178604  # type: ignore[method-assign]
    terminals._by_conversation.setdefault(conv_id, {})[("claude", "main")] = instance
    try:
        async with _runner_client(app) as client:
            await _create_session(client, conv_id)
            await registry.observe_required_terminal(
                conv_id, "claude", "main", instance, resource_role="claude-native"
            )
            await _fire("on_activity")
            await _fire("on_idle")
            assert app.state.has_active_work() is False
            # Printing the banner flips the pane back to running just before it dies.
            await _fire("on_activity")
            assert app.state.has_active_work() is True
            instance.last_pane_text = lambda: (  # type: ignore[method-assign]
                "Resume this session with:\nclaude --resume 0d5c8f3e"
            )
            instance.last_exit_status = lambda: 0  # type: ignore[method-assign]
            _session_event_queues_ref.pop(conv_id, None)
            await _fire("on_exit")
            await registry.wait_for_terminal_exit_cleanup()
            record = app.state.session_status_book.current(conv_id)
            assert record is not None
            assert (record.status, record.origin) == ("idle", StatusSource.RUNNER)
            assert app.state.has_active_work() is False
            queue = _session_event_queues_ref.get(conv_id)
            assert queue is not None
            published = [
                event.get("status")
                for event in (queue.get_nowait() for _ in range(queue.qsize()))
                if event.get("type") == "session.status"
            ]
            assert published == ["idle"]
    finally:
        _session_event_queues_ref.pop(conv_id, None)


@pytest.mark.asyncio
async def test_an_open_prompt_park_holds_the_runner_until_the_approval_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native prompt parked on a human holds the runner whatever the status says.

    A pane-status harness reads ``idle`` while its TUI waits on a mirrored
    prompt; the mirror's open park keeps the runner up until
    OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S.
    """
    from omnigent.native import prompt_parks
    from tests.runner.conftest import _runner_client

    monkeypatch.setenv(_APPROVAL_MAX_ENV, "600")
    park_clock = _Clock()
    monkeypatch.setattr(prompt_parks, "_clock", park_clock)
    conv_id = "conv_native_parked"
    app = await _native_app_with_session(conv_id, harness="goose-native")
    try:
        async with _runner_client(app) as client:
            await _create_session(client, conv_id)
            await _post_status(client, conv_id, "idle")
            assert app.state.has_active_work() is False
            prompt_parks.open_park(conv_id, "goose:1")
            park_clock.now += 599.0
            assert app.state.has_active_work() is True

            async def _bound_passes() -> None:
                park_clock.now += 1.0

            await _assert_monitor_blocked_then_shuts_down(
                has_active_work=app.state.has_active_work,
                release=_bound_passes,
            )
    finally:
        prompt_parks.clear_session(conv_id)


@pytest.mark.asyncio
async def test_a_reported_dialog_holds_the_runner_until_the_approval_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn parked on a dialog the agent reported outlives the claim ceiling.

    The silent ``running`` stops holding at the ceiling; its ``blocked_on``
    keeps the runner up until OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S after the
    dialog opened.
    """
    from tests.runner.conftest import _runner_client

    monkeypatch.setenv(_MAX_TURN_ENV, "3600")
    monkeypatch.setenv(_APPROVAL_MAX_ENV, "7200")
    clock = _Clock()
    conv_id = "conv_native_dialog"
    app = await _native_app_with_session(conv_id, harness="claude-native", status_clock=clock)
    async with _runner_client(app) as client:
        await _create_session(client, conv_id)
        await _post_status(client, conv_id, "running", blocked_on="permission prompt")
        clock.now += 3600.0
        assert app.state.has_active_work() is True
        clock.now += 3599.0
        assert app.state.has_active_work() is True

        async def _bound_passes() -> None:
            clock.now += 1.0

        await _assert_monitor_blocked_then_shuts_down(
            has_active_work=app.state.has_active_work,
            release=_bound_passes,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("max_turn_s", ["nan", "inf"])
async def test_a_non_finite_max_turn_knob_keeps_the_default_runner_hold(
    max_turn_s: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A nan or infinite OMNIGENT_NATIVE_PANE_MAX_TURN_S falls back to the default.

    No evidence age is below nan, so it would switch the hold off; an infinite
    ceiling would let a lost idle hold the runner forever.

    :param max_turn_s: The knob's value, e.g. ``"nan"``.
    """
    from omnigent.terminals.pane_reaper import _DEFAULT_MAX_TURN_S
    from tests.runner.conftest import _runner_client

    monkeypatch.setenv(_MAX_TURN_ENV, max_turn_s)
    clock = _Clock()
    conv_id = f"conv_native_{max_turn_s}_ceiling"
    with caplog.at_level(logging.WARNING, logger="omnigent.terminals.pane_reaper"):
        app = await _native_app_with_session(conv_id, harness="codex-native", status_clock=clock)
    assert any(
        f"{_MAX_TURN_ENV}={max_turn_s!r} is not a finite number" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
    async with _runner_client(app) as client:
        await _create_session(client, conv_id)
        await _post_status(client, conv_id, "running")
        assert app.state.has_active_work() is True
        clock.now += _DEFAULT_MAX_TURN_S - 1.0
        assert app.state.has_active_work() is True
        clock.now += 1.0
        assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_a_dialog_opening_and_closing_does_not_warn_twice_in_one_episode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An expired hold is warned about once per episode, not once per change.

    A dialog the agent reports and then clears changes the record but not its
    episode, so the expiry is not logged again. A new episode is.
    """
    from tests.runner.conftest import _runner_client

    monkeypatch.setenv(_MAX_TURN_ENV, "3600")
    monkeypatch.setenv(_APPROVAL_MAX_ENV, "600")
    clock = _Clock()
    conv_id = "conv_native_dialog_churn"
    app = await _native_app_with_session(conv_id, harness="codex-native", status_clock=clock)
    book = app.state.session_status_book
    async with _runner_client(app) as client:
        await _create_session(client, conv_id)
        await _post_status(client, conv_id, "running")
        since = book.current(conv_id).since
        clock.now += 3600.0
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            assert app.state.has_active_work() is False
            assert len(_events(caplog, "runner_in_flight_hold_expired")) == 1
            await _post_status(client, conv_id, "running", blocked_on="permission prompt")
            assert app.state.has_active_work() is True
            await _post_status(client, conv_id, "running")
            assert book.current(conv_id).since == since
            assert app.state.has_active_work() is False
            assert len(_events(caplog, "runner_in_flight_hold_expired")) == 1

            await _post_status(client, conv_id, "idle")
            await _post_status(client, conv_id, "running")
            clock.now += 3600.0
            assert app.state.has_active_work() is False
            assert app.state.has_active_work() is False
        assert len(_events(caplog, "runner_in_flight_hold_expired")) == 2


@pytest.mark.asyncio
async def test_a_new_dispatch_carries_a_recorded_turn_past_its_episodes_ceiling(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A runner dispatch is evidence of work even when the book already says ``running``.

    The new turn's ``running`` repeats the recorded one, so the episode keeps
    its start; only the dispatch keeps the runner up past that episode's
    ceiling, and the ceiling then counts from the dispatch.
    """
    from tests.runner.conftest import _runner_client

    monkeypatch.setenv(_MAX_TURN_ENV, str(_CEILING_S))
    clock = _Clock()
    conv_id = "conv_native_redispatched"
    app = await _native_app_with_session(conv_id, harness="codex-native", status_clock=clock)
    book = app.state.session_status_book
    async with _runner_client(app) as client:
        await _create_session(client, conv_id)
        await _post_status(client, conv_id, "running")
        episode_start = book.current(conv_id).since
        clock.now += _CEILING_S - 60.0
        await _dispatch_turn(client, app, conv_id)
        dispatched_at = book.last_dispatch_at(conv_id)
        assert dispatched_at == clock.now
        record = book.current(conv_id)
        assert record.status == "running" and record.since == episode_start

        clock.now = episode_start + _CEILING_S + 60.0
        assert app.state.has_active_work() is True
        clock.now = dispatched_at + _CEILING_S - 1.0
        assert app.state.has_active_work() is True
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            clock.now += 1.0
            assert app.state.has_active_work() is False
        expired = _events(caplog, "runner_in_flight_hold_expired")
        assert len(expired) == 1
        assert expired[0].attributes["evidence_age_s"] == _CEILING_S  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_an_sdk_sessions_recorded_waits_do_not_hold_the_runner() -> None:
    """Only a native session's recorded dialog or prompt park holds the runner.

    An SDK turn holds the runner through ``active_turns`` while it runs; a
    dialog relayed for the session afterwards, or a park opened under its id,
    is not a native turn in flight.
    """
    from omnigent.native import prompt_parks
    from tests.runner.conftest import _runner_client

    conv_id = "conv_sdk_waits"
    app = await _native_app_with_session(conv_id, harness="openai-agents")
    try:
        async with _runner_client(app) as client:
            await _create_session(client, conv_id)
            await _post_status(client, conv_id, "running", blocked_on="permission prompt")
            assert app.state.session_status_book.blocked(conv_id) is not None
            prompt_parks.open_park(conv_id, "sdk:1")
            assert prompt_parks.oldest_open_age_s(conv_id) is not None
            assert conv_id not in app.state.active_turns
            assert app.state.has_active_work() is False
    finally:
        prompt_parks.clear_session(conv_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("max_turn_s", ["0", "60"])
async def test_a_small_max_turn_knob_cannot_switch_the_runner_hold_off(
    max_turn_s: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OMNIGENT_NATIVE_PANE_MAX_TURN_S below an hour is floored for the runner.

    :param max_turn_s: The knob's value, e.g. ``"0"``.
    """
    from tests.runner.conftest import _runner_client

    monkeypatch.setenv(_MAX_TURN_ENV, max_turn_s)
    clock = _Clock()
    conv_id = "conv_native_clamped"
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        app = await _native_app_with_session(conv_id, harness="codex-native", status_clock=clock)
    assert len(_events(caplog, "runner_in_flight_hold_ceiling_clamped")) == 1
    async with _runner_client(app) as client:
        await _create_session(client, conv_id)
        await _post_status(client, conv_id, "running")
        clock.now += 3599.0
        assert app.state.has_active_work() is True
        clock.now += 1.0
        assert app.state.has_active_work() is False


# ── losing or tearing down the pane ──


async def _plant_pane_sidecars(app: FastAPI, conv_id: str, bridge_dir: Path, key: str) -> Any:
    """Plant *key*'s pane sidecars, minus claude's prompt waiter.

    A live prompt waiter holds the runner by itself, which would hide the
    status hold these tests pin.

    :returns: The rig's ``PlantedSidecars``.
    """
    from tests.terminals.native_pane_rig import plant_sidecars

    sidecars = plant_sidecars(app, conv_id, bridge_dir, harness_key=key)
    waiter = app.state.claude_prompt_waiters.pop(conv_id)
    waiter.cancel()
    await asyncio.wait({waiter})
    return sidecars


def _kept(sidecars: Any) -> list[str]:
    """What a kept set of the planted sidecars holds."""
    return [name for name in sidecars.planted if name != "claude_prompt_waiter"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ends_by", ["relayed_idle", "ceiling", "launching_session_deleted"])
@pytest.mark.parametrize("rotated", [False, True], ids=["own", "rotated"])
@pytest.mark.parametrize("how", ["deleted", "exited"])
@pytest.mark.parametrize("key", ["codex", "opencode"])
async def test_a_turn_that_outlives_its_tui_holds_the_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    how: str,
    rotated: bool,
    ends_by: str,
) -> None:
    """Codex and opencode run a turn in their vendor server, not in the TUI.

    Losing the TUI mid-turn, to a user's DELETE or a crash, keeps the turn's
    relayed ``running`` while the server lives, so the runner stays up for
    it. The same holds for a TUI that a ``/clear`` rotation moved to another
    session: its turn still runs in the launching session's server. The
    turn's relayed ``idle`` ends the hold, so does deleting the launching
    session (which releases its server), and with no evidence of work the
    ceiling bounds it like any silent claim.

    :param key: Native harness key, e.g. ``"codex"``.
    :param how: How the TUI went away.
    :param rotated: Whether a rotation moved the TUI before it was lost.
    :param ends_by: What ends the runner's hold.
    """
    from omnigent.entities.session_resources import terminal_resource_id
    from omnigent.runner.app import _session_event_queues_ref
    from tests.runner.conftest import _runner_client, _spec_resolver_returning
    from tests.terminals.native_pane_rig import build_pane_rig

    monkeypatch.setenv(_MAX_TURN_ENV, str(_CEILING_S))
    clock = _Clock()
    rig = await build_pane_rig(
        tmp_path,
        monkeypatch,
        key=key,
        status_clock=clock,
        spec_resolver=await _spec_resolver_returning(_native_spec(f"{key}-native")),
    )
    app, launching = rig.app, rig.conv_id
    session = f"{launching}_rotated" if rotated else launching
    name = rig.agent.terminal_name
    terminal_id = terminal_resource_id(name, "main")
    sidecars = await _plant_pane_sidecars(app, launching, tmp_path, key)
    try:
        async with _runner_client(app) as client:
            await _create_session(client, launching)
            if rotated:
                await _create_session(client, session)
                resp = await client.post(
                    f"/v1/sessions/{launching}/resources/terminals/{terminal_id}/transfer",
                    json={"target_session_id": session},
                )
                assert resp.status_code == 200, resp.text
                assert rig.resources.sidecar_home(session) == launching
            await _post_status(client, session, "running")
            if how == "deleted":
                resp = await client.delete(
                    f"/v1/sessions/{session}/resources/terminals/{terminal_id}"
                )
                assert resp.status_code == 200, resp.text
                releases = set(app.state.native_sidecar_release_tasks)
                assert releases
                await asyncio.wait(releases, timeout=10)
            else:
                await rig.fire("on_exit")
                await rig.resources.wait_for_terminal_exit_cleanup()
            assert rig.terminal_registry.get(session, name, "main") is None
            assert sidecars.leftovers(app) == _kept(sidecars)
            assert app.state.has_active_work() is True
            clock.now += _CEILING_S - 1.0
            assert app.state.has_active_work() is True

            async def _turn_ends() -> None:
                if ends_by == "relayed_idle":
                    await _post_status(client, session, "idle")
                elif ends_by == "launching_session_deleted":
                    resp = await client.delete(f"/v1/sessions/{launching}")
                    assert resp.status_code == 200, resp.text
                    assert rig.book.current(session) is None
                else:
                    clock.now += 1.0

            await _assert_monitor_blocked_then_shuts_down(
                has_active_work=app.state.has_active_work,
                release=_turn_ends,
            )
    finally:
        sidecars.discard()
        rig.drain()
        _session_event_queues_ref.pop(session, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("launching_server", ["gone", "alive"])
@pytest.mark.parametrize("how", ["deleted", "exited"])
async def test_a_tui_moved_into_a_session_with_its_own_vendor_server_follows_the_launching_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str, launching_server: str
) -> None:
    """The rotated session's own vendor server never keeps a moved TUI's turn.

    Here the rotated session already has a codex app-server of its own when a
    ``/clear`` rotation moves the launching session's TUI to it, but the moved
    TUI's turn runs in the launching session's server. With that server gone,
    losing the TUI resets the rotated session at once. With it alive, the
    status is kept until the launching session's server is released (here by
    deleting that session), while the rotated session's own server lives on.

    :param how: How the TUI went away.
    :param launching_server: Whether the launching session's app-server is
        still registered when the TUI is lost.
    """
    from omnigent.entities.session_resources import terminal_resource_id
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.runner.native import orchestration
    from tests.runner.conftest import _runner_client, _spec_resolver_returning
    from tests.terminals.native_pane_rig import build_pane_rig

    monkeypatch.setenv(_MAX_TURN_ENV, str(_CEILING_S))
    clock = _Clock()
    rig = await build_pane_rig(
        tmp_path,
        monkeypatch,
        key="codex",
        status_clock=clock,
        spec_resolver=await _spec_resolver_returning(_native_spec("codex-native")),
    )
    app, launching = rig.app, rig.conv_id
    rotated = f"{launching}_rotated"
    (tmp_path / "rotated").mkdir()
    sidecars = await _plant_pane_sidecars(app, launching, tmp_path, "codex")
    own = await _plant_pane_sidecars(app, rotated, tmp_path / "rotated", "codex")
    terminal_id = terminal_resource_id("codex", "main")
    try:
        async with _runner_client(app) as client:
            await _create_session(client, launching)
            await _create_session(client, rotated)
            resp = await client.post(
                f"/v1/sessions/{launching}/resources/terminals/{terminal_id}/transfer",
                json={"target_session_id": rotated},
            )
            assert resp.status_code == 200, resp.text
            assert rig.resources.sidecar_home(rotated) == launching
            await _post_status(client, rotated, "running")
            if launching_server == "gone":
                await orchestration.teardown_codex_native_app_server(launching)
                assert launching not in orchestration._AUTO_CODEX_APP_SERVERS
            if how == "deleted":
                resp = await client.delete(
                    f"/v1/sessions/{rotated}/resources/terminals/{terminal_id}"
                )
                assert resp.status_code == 200, resp.text
                releases = set(app.state.native_sidecar_release_tasks)
                if releases:
                    await asyncio.wait(releases, timeout=10)
            else:
                await rig.fire("on_exit")
                await rig.resources.wait_for_terminal_exit_cleanup()
            assert rig.terminal_registry.get(rotated, "codex", "main") is None
            assert rotated in orchestration._AUTO_CODEX_APP_SERVERS
            if launching_server == "alive":
                record = rig.book.current(rotated)
                assert record is not None and record.status == "running"
                assert app.state.has_active_work() is True
                resp = await client.delete(f"/v1/sessions/{launching}")
                assert resp.status_code == 200, resp.text
                assert rotated in orchestration._AUTO_CODEX_APP_SERVERS
            assert rig.book.current(rotated) is None
            assert app.state.has_active_work() is False
    finally:
        sidecars.discard()
        own.discard()
        rig.drain()
        _session_event_queues_ref.pop(rotated, None)


@pytest.mark.asyncio
async def test_reaping_the_launching_sessions_own_new_pane_resets_the_status_kept_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launching session's pane reap releases its server, so it ends a kept status.

    A ``/clear`` rotation moved the launching session's codex TUI away and that
    TUI was lost mid-turn, so the rotated session keeps its ``running`` for the
    launching session's app-server. The launching session then gets a pane of
    its own again. Reaping that pane closes it (which resets the launching
    session's own status) and releases its sidecars, app-server included, so
    the rotated session's kept status is reset too and the runner is freed.
    """
    from omnigent.entities.session_resources import terminal_resource_id
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.runner.native import orchestration
    from tests.runner.conftest import _runner_client, _spec_resolver_returning
    from tests.runner.helpers import make_test_terminal_instance
    from tests.terminals.native_pane_rig import build_pane_rig

    monkeypatch.setenv(_MAX_TURN_ENV, str(_CEILING_S))
    rig = await build_pane_rig(
        tmp_path,
        monkeypatch,
        key="codex",
        spec_resolver=await _spec_resolver_returning(_native_spec("codex-native")),
    )
    app, launching = rig.app, rig.conv_id
    rotated = f"{launching}_rotated"
    terminal_id = terminal_resource_id("codex", "main")
    sidecars = await _plant_pane_sidecars(app, launching, tmp_path, "codex")
    try:
        async with _runner_client(app) as client:
            await _create_session(client, launching)
            await _create_session(client, rotated)
            resp = await client.post(
                f"/v1/sessions/{launching}/resources/terminals/{terminal_id}/transfer",
                json={"target_session_id": rotated},
            )
            assert resp.status_code == 200, resp.text
            await _post_status(client, rotated, "running")
            await rig.fire("on_exit")
            await rig.resources.wait_for_terminal_exit_cleanup()
            assert rig.resources._vendor_turn_homes == {rotated: launching}
            assert app.state.has_active_work() is True

            relaunched = make_test_terminal_instance("codex", "main", tmp_path / "relaunch")

            async def _close() -> None:
                relaunched.running = False

            relaunched.close = _close  # type: ignore[method-assign]
            relaunched.start_idle_watcher_thread = lambda **_kwargs: None  # type: ignore[method-assign]
            panes = rig.terminal_registry._by_conversation.setdefault(launching, {})
            panes[("codex", "main")] = relaunched
            await rig.resources.observe_auxiliary_terminal(
                launching, "codex", "main", relaunched, resource_role="codex-native"
            )
            await _post_status(client, launching, "idle")

            assert await rig.reaper._reap(rig.pane) is True
            assert launching not in orchestration._AUTO_CODEX_APP_SERVERS
            assert rig.book.current(rotated) is None
            assert rig.resources._vendor_turn_homes == {}
            assert app.state.has_active_work() is False
    finally:
        sidecars.discard()
        rig.drain()
        _session_event_queues_ref.pop(rotated, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["exit", "close"])
@pytest.mark.parametrize("key", ["codex", "opencode"])
async def test_an_edge_recorded_while_the_old_pane_closes_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, path: str
) -> None:
    """A new turn's edge that lands while the old TUI is being closed survives it.

    Codex and opencode observe their TUI as auxiliary; with no vendor server
    registered, losing it resets the session's status. The registry frees the
    pane's key before the old tmux is killed, so a new turn can start and
    record its first edge meanwhile. The dead pane's reset spares a record
    that changed during the close, on the exit path as on a close, and the
    runner keeps holding for the new turn. The mark protects auxiliary TUIs
    only: after a REQUIRED pane's exit, the exit publisher decides the
    session's status (a required exit ends the session).

    :param key: Native harness key, e.g. ``"codex"``.
    :param path: ``exit`` (the pane died) or ``close`` (it was closed).
    """
    from omnigent.entities.session_resources import terminal_resource_id
    from omnigent.runner.native import orchestration
    from tests.runner.conftest import _runner_client, _spec_resolver_returning
    from tests.terminals.native_pane_rig import build_pane_rig

    rig = await build_pane_rig(
        tmp_path,
        monkeypatch,
        key=key,
        spec_resolver=await _spec_resolver_returning(_native_spec(f"{key}-native")),
    )
    app, conv_id = rig.app, rig.conv_id
    name = rig.agent.terminal_name
    assert not orchestration.native_vendor_server_registered(conv_id, f"{key}-native")
    instance = rig.terminal_registry.get(conv_id, name, "main")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow_close() -> None:
        entered.set()
        await release.wait()
        instance.running = False

    instance.close = _slow_close  # type: ignore[method-assign]
    try:
        async with _runner_client(app) as client:
            await _create_session(client, conv_id)
            await _post_status(client, conv_id, "running")
            if path == "exit":
                await rig.fire("on_exit")
                closer = asyncio.ensure_future(rig.resources.wait_for_terminal_exit_cleanup())
            else:
                closer = asyncio.ensure_future(
                    rig.resources.close_terminal(conv_id, terminal_resource_id(name, "main"))
                )
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert not rig.alive()  # the key is already free for a successor
            await _post_status(client, conv_id, "running")
            release.set()
            await asyncio.wait_for(closer, timeout=5)
            record = rig.book.current(conv_id)
            assert record is not None and record.status == "running"
            assert app.state.has_active_work() is True
    finally:
        release.set()
        rig.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["codex", "opencode", "pi"])
async def test_deleting_an_idle_tui_releases_its_sidecars_and_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """A DELETE whose sidecars nothing needs releases them and the runner's hold.

    The session's ``waiting`` (sub-agents still working when the turn ended)
    is no claim on the pane, so the sidecars are released, codex's and
    opencode's vendor servers included, and the release resets the status
    the close kept for them.

    :param key: Native harness key, e.g. ``"codex"``.
    """
    from omnigent.entities.session_resources import terminal_resource_id
    from tests.runner.conftest import _runner_client, _spec_resolver_returning
    from tests.terminals.native_pane_rig import build_pane_rig

    rig = await build_pane_rig(
        tmp_path,
        monkeypatch,
        key=key,
        spec_resolver=await _spec_resolver_returning(_native_spec(f"{key}-native")),
    )
    app, conv_id = rig.app, rig.conv_id
    sidecars = await _plant_pane_sidecars(app, conv_id, tmp_path, key)
    try:
        async with _runner_client(app) as client:
            await _create_session(client, conv_id)
            await _post_status(client, conv_id, "waiting")
            assert app.state.has_active_work() is True
            terminal_id = terminal_resource_id(rig.agent.terminal_name, "main")
            resp = await client.delete(f"/v1/sessions/{conv_id}/resources/terminals/{terminal_id}")
            assert resp.status_code == 200, resp.text
            await asyncio.wait(set(app.state.native_sidecar_release_tasks), timeout=10)
            assert sidecars.leftovers(app) == []
            assert app.state.session_status_book.current(conv_id) is None
            assert app.state.has_active_work() is False
    finally:
        sidecars.discard()
        rig.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["codex", "pi"])
async def test_a_reap_ends_the_runner_hold_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """A lost closing edge holds the runner only until the reaper tears the pane down.

    The reap closes the pane and releases its sidecars, a codex app-server
    included, and resets the session's status, so nothing is left to hold
    the runner.

    :param key: Native harness key, e.g. ``"codex"``.
    """
    from tests.runner.conftest import _runner_client, _spec_resolver_returning
    from tests.terminals.native_pane_rig import build_pane_rig

    rig = await build_pane_rig(
        tmp_path,
        monkeypatch,
        key=key,
        spec_resolver=await _spec_resolver_returning(_native_spec(f"{key}-native")),
    )
    app, conv_id = rig.app, rig.conv_id
    sidecars = await _plant_pane_sidecars(app, conv_id, tmp_path, key)
    try:
        async with _runner_client(app) as client:
            await _create_session(client, conv_id)
            await _post_status(client, conv_id, "running")
            assert app.state.has_active_work() is True

            async def _reap() -> None:
                assert await rig.reaper._reap(rig.pane) is True

            await _assert_monitor_blocked_then_shuts_down(
                has_active_work=app.state.has_active_work,
                release=_reap,
            )
            assert sidecars.leftovers(app) == []
    finally:
        sidecars.discard()
        rig.drain()
