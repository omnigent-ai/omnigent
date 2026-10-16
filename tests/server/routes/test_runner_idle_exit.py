"""A runner's idle-reaper exit settles its sessions instead of failing them."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, MutableMapping
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import pytest
import uvicorn
from fastapi import FastAPI

from omnigent.runner.identity import token_bound_runner_id
from omnigent.runner.transports.ws_tunnel.frames import RUNNER_IDLE_EXIT_CLOSE_REASON
from omnigent.runner.transports.ws_tunnel.registry import RunnerSession, TunnelRegistry
from omnigent.runner.transports.ws_tunnel.serve import serve_tunnel
from omnigent.runtime import session_stream
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.runner_tunnel import create_runner_tunnel_router
from omnigent.server.schemas import ErrorDetail
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_TASK_TIMEOUT_S = 10.0
_RUNNER = "runner-idle-exit"
_DISCONNECTED = ErrorDetail(
    code="runner_disconnected", message="Runner disconnected unexpectedly."
)


def _event_attributes(
    caplog: pytest.LogCaptureFixture, event_name: str
) -> list[dict[str, object]]:
    return [
        cast("dict[str, object]", getattr(record, "attributes", {}))
        for record in caplog.records
        if getattr(record, "event_name", None) == event_name
    ]


async def _idle_runner_app(
    scope: MutableMapping[str, Any],
    receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
    send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
) -> None:
    """Runner ASGI app that is never called: the test tunnel carries no requests."""
    raise AssertionError(f"unexpected runner request: {scope.get('path')}")


async def test_idle_reaper_close_reaches_the_server_disconnect_hook() -> None:
    """The runner's graceful idle shutdown arrives at the server as the idle-exit reason."""
    registry = TunnelRegistry()
    disconnected: asyncio.Future[RunnerSession] = asyncio.get_running_loop().create_future()

    async def on_disconnect(runner_id: str, session: RunnerSession) -> None:
        del runner_id
        if not disconnected.done():
            disconnected.set_result(session)

    app = FastAPI()
    router = create_runner_tunnel_router(registry, on_runner_disconnect=on_disconnect)
    app.include_router(router, prefix="/v1")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server_task = asyncio.create_task(server.serve())
    tunnel_token = "idle-exit-test-token"
    runner_id = token_bound_runner_id(tunnel_token)
    shutdown = asyncio.Event()
    runner_task: asyncio.Task[None] | None = None
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        runner_task = asyncio.create_task(
            serve_tunnel(
                _idle_runner_app,
                server_url=f"http://127.0.0.1:{port}",
                runner_id=runner_id,
                runner_version="0.0.0-test",
                tunnel_token=tunnel_token,
                shutdown_event=shutdown,
            )
        )
        async with asyncio.timeout(_TASK_TIMEOUT_S):
            while registry.get(runner_id) is None:
                await asyncio.sleep(0.01)
            shutdown.set()
            await runner_task
            session = await disconnected

        assert session.peer_close_reason == RUNNER_IDLE_EXIT_CLOSE_REASON
    finally:
        if runner_task is not None and not runner_task.done():
            runner_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner_task
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=_TASK_TIMEOUT_S)


@pytest.fixture
def bound_sessions(db_uri: str) -> Iterator[tuple[SqlAlchemyConversationStore, dict[str, str]]]:
    """Stored sessions of one runner with no local relay, plus one on another runner."""
    store = SqlAlchemyConversationStore(db_uri)
    ids: dict[str, str] = {}
    for name, runner_id, status in (
        ("running", _RUNNER, "running"),
        ("waiting", _RUNNER, "waiting"),
        ("elsewhere", "runner-other", "running"),
    ):
        row = store.create_conversation()
        ids[name] = row.id
        store.set_runner_id(row.id, runner_id)
        store.set_session_live_status(row.id, status)
        sessions._session_status_cache[row.id] = status
    try:
        yield store, ids
    finally:
        for session_id in ids.values():
            sessions._intentional_stop_sessions.pop(session_id, None)
            sessions._session_status_cache.pop(session_id, None)
            session_stream.close(session_id)


@pytest.mark.parametrize("idle_exit", [True, False])
async def test_offline_sweep_settles_an_idle_exited_runners_sessions(
    bound_sessions: tuple[SqlAlchemyConversationStore, dict[str, str]],
    caplog: pytest.LogCaptureFixture,
    idle_exit: bool,
) -> None:
    """Without the idle exit the same sweep fails the runner's mid-turn sessions."""
    caplog.set_level(logging.INFO)
    store, ids = bound_sessions
    affected = store.list_conversations_by_runner_id(_RUNNER)
    if idle_exit:
        orchestration._note_runner_idle_exit(_RUNNER, affected)

    await sessions._mark_runner_sessions_offline(affected, _DISCONNECTED, store)

    expected = "idle" if idle_exit else "failed"
    for name in ("running", "waiting"):
        assert sessions._session_status_cache[ids[name]] == expected
        stored = store.get_conversation(ids[name])
        assert stored is not None
        error = sessions._last_task_error_from_labels(stored.labels)
        assert (error is None) is idle_exit
    assert ids["elsewhere"] not in sessions._intentional_stop_sessions
    mismatches = _event_attributes(caplog, "runner_idle_exit_status_mismatch")
    assert sorted(cast("str", event["cached_session_status"]) for event in mismatches) == (
        ["running", "waiting"] if idle_exit else []
    )


def test_idle_exit_marks_relay_bound_sessions_of_that_runner_only() -> None:
    relays = {
        "relay-running": SimpleNamespace(runner_id=_RUNNER),
        "relay-idle": SimpleNamespace(runner_id=_RUNNER),
        "relay-other": SimpleNamespace(runner_id="runner-other"),
    }
    try:
        for session_id, handle in relays.items():
            sessions._runner_relay_tasks[session_id] = handle  # type: ignore[assignment]
        sessions._session_status_cache["relay-running"] = "running"
        sessions._session_status_cache["relay-idle"] = "idle"

        orchestration._note_runner_idle_exit(_RUNNER)

        assert {
            session_id
            for session_id in relays
            if sessions._intentional_stop_sessions.get(session_id) == _RUNNER
        } == {"relay-running", "relay-idle"}
    finally:
        for session_id in relays:
            sessions._runner_relay_tasks.pop(session_id, None)
            sessions._session_status_cache.pop(session_id, None)
            sessions._intentional_stop_sessions.pop(session_id, None)


class _DropStreamResponse:
    """Runner stream that sends its ready heartbeat, then loses the tunnel."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def __aenter__(self) -> _DropStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    def raise_for_status(self) -> None:
        """The stream represents a successful HTTP response."""

    async def aiter_text(self) -> AsyncIterator[str]:
        yield 'data: {"type": "session.heartbeat"}\n\n'
        await self._gate.wait()
        raise ConnectionError("tunnel closed before request completed")


class _DropRunnerClient:
    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    def stream(self, method: str, path: str, *, timeout: Any) -> _DropStreamResponse:
        del method, path, timeout
        return _DropStreamResponse(self._gate)


class _LabelStore:
    def __init__(self) -> None:
        self.labels: dict[str, dict[str, str]] = {}

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        self.labels.setdefault(conversation_id, {}).update(updates)

    def get_runner_liveness(self, conversation_id: str) -> None:
        return None

    def get_conversation(self, conversation_id: str) -> None:
        return None


@pytest.mark.parametrize("idle_exit", [True, False])
async def test_relay_settles_an_idle_exited_runners_session_instead_of_failing_it(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    idle_exit: bool,
) -> None:
    """A session the server still caches as running settles idle after the runner's idle exit."""
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.0)
    caplog.set_level(logging.INFO)
    sessions._runner_relay_tasks.clear()
    session_id = "0123456789abcdef0123456789abcdef"
    gate = asyncio.Event()
    try:
        handle = await sessions._ensure_runner_relay_ready(
            session_id,
            _RUNNER,
            _DropRunnerClient(gate),  # type: ignore[arg-type]
            conversation_store=_LabelStore(),  # type: ignore[arg-type]
        )
        assert handle is not None
        sessions._session_status_cache[session_id] = "running"
        if idle_exit:
            orchestration._note_runner_idle_exit(_RUNNER)

        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        assert handle.task.exception() is None
        expected_status = "idle" if idle_exit else "failed"
        assert sessions._session_status_cache.get(session_id) == expected_status
        [outcome] = _event_attributes(caplog, "runner_stream_disconnected")
        assert outcome["decision"] == ("intentional_stop" if idle_exit else "failed_mid_turn")
    finally:
        gate.set()
        handle = sessions._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions._runner_relay_tasks.clear()
        sessions._session_status_cache.pop(session_id, None)
        sessions._intentional_stop_sessions.pop(session_id, None)
        session_stream.close(session_id)
