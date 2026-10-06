"""A relay holds a silently dropped runner's turn open while its host is also away."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx
import pytest

from omnigent.runtime import session_stream
from omnigent.server import runner_drop_state, shutdown_state
from omnigent.server.routes._sessions import orchestration
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.budgets import budget

_RUNNER_ID = "runner-relay-silent-drop"
_HOST_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
_NORMAL_GRACE_S = 0.1
_DONE_STREAM = (
    'data: {"type":"session.heartbeat"}\n\n'
    'data: {"type":"session.status","status":"idle"}\n\n'
    "data: [DONE]\n\n"
)


class _TunnelLikeTransport(httpx.AsyncBaseTransport):
    """Models ``WSTunnelTransport``: refuses requests until the runner registers.

    :param online: Whether the runner is registered.
    """

    def __init__(self, *, online: bool = False) -> None:
        self.online = online
        self.attempts = 0
        self.waits: list[float] = []
        self.respond: Callable[[httpx.Request], httpx.Response] = lambda _request: httpx.Response(
            200, text=_DONE_STREAM
        )
        self._registered = asyncio.Event()
        # Set by the zero-length "is it registered?" check that opens the extended wait,
        # and by the first bounded wait after it, once the host has read offline.
        self.extension_started = asyncio.Event()
        self.extension_waiting = asyncio.Event()

    def register(self) -> None:
        self.online = True
        self._registered.set()

    async def wait_for_runner(self, timeout_s: float) -> bool:
        self.waits.append(timeout_s)
        if timeout_s <= 0:
            self.extension_started.set()
            return self.online
        if self.extension_started.is_set():
            self.extension_waiting.set()
        if self.online:
            return True
        try:
            await asyncio.wait_for(self._registered.wait(), timeout_s)
        except asyncio.TimeoutError:
            return False
        return True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if not self.online:
            raise httpx.ConnectError("runner is offline", request=request)
        return self.respond(request)


@dataclass
class _RelayCase:
    store: SqlAlchemyConversationStore
    session_id: str
    client: httpx.AsyncClient
    transport: _TunnelLikeTransport
    host_online: threading.Event

    def start(self) -> asyncio.Task[None]:
        return asyncio.create_task(
            orchestration._relay_runner_stream(
                self.session_id, self.client, self.store, runner_id=_RUNNER_ID
            )
        )

    def status(self) -> str | None:
        return orchestration._session_status_cache.get(self.session_id)

    def last_error_code(self) -> str | None:
        conv = self.store.get_conversation(self.session_id)
        assert conv is not None
        return conv.labels.get("omnigent.last_task_error_code") or None


@pytest.fixture
async def relay_case(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> AsyncIterator[_RelayCase]:
    """A mid-turn, host-bound session with short grace windows and an offline host."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", _NORMAL_GRACE_S)
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_GRACE_S", 0.8)
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_RECHECK_S", 0.05)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.02)
    host_online = threading.Event()
    monkeypatch.setattr(
        runner_drop_state, "_host_online_probe", lambda _host_id: host_online.is_set()
    )
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(runner_id=_RUNNER_ID)
    store.set_host_id(conversation.id, _HOST_ID, workspace="/tmp/relay-silent-drop")
    session_id = conversation.id
    orchestration._session_status_cache[session_id] = "running"
    transport = _TunnelLikeTransport()
    try:
        async with httpx.AsyncClient(base_url="http://runner", transport=transport) as client:
            yield _RelayCase(store, session_id, client, transport, host_online)
    finally:
        orchestration._intentional_stop_sessions.pop(session_id, None)
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        shutdown_state.reset_for_tests()
        session_stream.close(session_id)


def _rows(caplog: pytest.LogCaptureFixture, event_name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event_name", None) == event_name]


async def test_silent_drop_with_the_host_offline_fails_only_after_the_silent_grace(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture
) -> None:
    drop = runner_drop_state.note(_RUNNER_ID, "silent")
    task = relay_case.start()

    # The relay is past its normal grace and waiting for the host; nothing has failed.
    await asyncio.wait_for(relay_case.transport.extension_waiting.wait(), budget(5.0))
    assert not task.done()
    assert relay_case.status() == "running"

    await asyncio.wait_for(task, budget(5.0))
    assert time.monotonic() - drop.dropped_at >= 0.8, "failed before the silent grace ran out"
    assert relay_case.status() == "failed"
    assert relay_case.last_error_code() == "runner_disconnected"
    # The host was rechecked while the relay waited.
    assert sum(1 for t in relay_case.transport.waits if 0 < t <= 0.05 + 1e-6) >= 2

    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.session_id == relay_case.session_id
    assert grace.attributes["path"] == "relay"
    assert grace.attributes["outcome"] == "expired"
    assert grace.attributes["extended"] is True
    assert grace.attributes["drop_kind"] == "silent"
    assert grace.attributes["host_online"] is False
    assert grace.attributes["grace_s"] == 0.8
    assert grace.attributes["waited_s"] >= 0.8 - _NORMAL_GRACE_S
    # Every row about this outage names how the tunnel dropped.
    (lost,) = _rows(caplog, "runner_stream_transport_lost")
    (gave_up,) = _rows(caplog, "runner_stream_disconnected")
    (failed,) = _rows(caplog, "session_turn_failed")
    assert lost.attributes["drop_kind"] == "silent"
    assert gave_up.attributes["drop_kind"] == "silent"
    assert gave_up.attributes["decision"] == "failed_mid_turn"
    assert failed.attributes["drop_kind"] == "silent"
    assert failed.attributes["origin"] == "runner_disconnected_mid_turn"


async def test_runner_returning_within_the_silent_grace_resumes_the_stream(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture
) -> None:
    runner_drop_state.note(_RUNNER_ID, "silent")
    task = relay_case.start()
    await asyncio.wait_for(relay_case.transport.extension_started.wait(), budget(5.0))

    # The laptop wakes: the tunnel registers (which clears the drop) and the stream comes back.
    runner_drop_state.clear(_RUNNER_ID)
    relay_case.transport.register()
    await asyncio.wait_for(task, budget(5.0))

    assert relay_case.status() == "idle", "the resumed stream delivered the turn's end"
    assert relay_case.last_error_code() is None
    assert not _rows(caplog, "session_turn_failed")
    assert not _rows(caplog, "runner_stream_disconnected")
    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["outcome"] == "reconnected"
    assert grace.attributes["extended"] is True
    assert grace.attributes["drop_kind"] == "silent"


async def test_host_back_without_its_runner_ends_the_wait_and_fails_the_turn(
    relay_case: _RelayCase,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Far longer than the test can wait, so only the host's return can end it.
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_GRACE_S", 60.0)
    runner_drop_state.note(_RUNNER_ID, "silent")
    task = relay_case.start()
    await asyncio.wait_for(relay_case.transport.extension_waiting.wait(), budget(5.0))
    assert not task.done()

    relay_case.host_online.set()
    await asyncio.wait_for(task, budget(5.0))

    assert relay_case.status() == "failed"
    assert relay_case.last_error_code() == "runner_disconnected"
    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["outcome"] == "host_back_runner_missing"
    assert grace.attributes["extended"] is True
    assert grace.attributes["host_online"] is True
    assert grace.attributes["waited_s"] < 30.0


@pytest.mark.parametrize("recorded", ["sudden", None])
async def test_sudden_or_unrecorded_drop_keeps_the_normal_grace(
    relay_case: _RelayCase,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    recorded: str | None,
) -> None:
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_GRACE_S", 60.0)
    if recorded is not None:
        runner_drop_state.note(_RUNNER_ID, "sudden")

    # Fails on the normal grace, nowhere near the silent one.
    await asyncio.wait_for(relay_case.start(), budget(5.0))

    assert relay_case.status() == "failed"
    assert 0.0 not in relay_case.transport.waits, "the host of a vanished runner is not consulted"
    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["outcome"] == "expired"
    assert grace.attributes["extended"] is False
    assert grace.attributes["grace_s"] == _NORMAL_GRACE_S
    assert grace.attributes.get("drop_kind") == recorded
    (failed,) = _rows(caplog, "session_turn_failed")
    assert failed.attributes.get("drop_kind") == recorded


async def test_silent_drop_with_the_host_online_keeps_the_normal_grace(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_GRACE_S", 60.0)
    relay_case.host_online.set()
    runner_drop_state.note(_RUNNER_ID, "silent")

    await asyncio.wait_for(relay_case.start(), budget(5.0))

    assert relay_case.status() == "failed"
    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["outcome"] == "expired"
    assert grace.attributes["extended"] is False
    assert grace.attributes["host_online"] is True
    assert grace.attributes["drop_kind"] == "silent"


async def test_a_registered_runner_whose_stream_errors_is_not_held(
    relay_case: _RelayCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale silent record must not turn an HTTP error on a live runner into an endless retry."""
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_GRACE_S", 60.0)
    relay_case.transport.register()
    relay_case.transport.respond = lambda _request: httpx.Response(503)
    runner_drop_state.note(_RUNNER_ID, "silent")

    await asyncio.wait_for(relay_case.start(), budget(5.0))

    assert relay_case.status() == "failed"
    assert relay_case.last_error_code() == "runner_disconnected"


async def test_intentional_stop_is_never_held(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture
) -> None:
    orchestration._intentional_stop_sessions[relay_case.session_id] = _RUNNER_ID
    runner_drop_state.note(_RUNNER_ID, "silent")

    await asyncio.wait_for(relay_case.start(), budget(5.0))

    assert relay_case.status() == "idle"
    assert relay_case.last_error_code() is None
    assert 0.0 not in relay_case.transport.waits
    assert not _rows(caplog, "runner_disconnect_grace")


async def test_server_shutdown_is_never_held(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture
) -> None:
    shutdown_state.mark_server_shutting_down()
    runner_drop_state.note(_RUNNER_ID, "silent")

    await asyncio.wait_for(relay_case.start(), budget(5.0))

    assert relay_case.status() == "running", "a server that closed the tunnel fails nothing"
    assert 0.0 not in relay_case.transport.waits
    (gave_up,) = _rows(caplog, "runner_stream_disconnected")
    assert gave_up.attributes["decision"] == "server_shutdown"


async def test_a_client_without_a_tunnel_transport_is_not_held(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("runner is offline", request=request)

    runner_drop_state.note(_RUNNER_ID, "silent")
    async with httpx.AsyncClient(
        base_url="http://runner", transport=httpx.MockTransport(refuse)
    ) as client:
        await asyncio.wait_for(
            orchestration._relay_runner_stream(
                relay_case.session_id, client, relay_case.store, runner_id=_RUNNER_ID
            ),
            budget(5.0),
        )

    assert relay_case.status() == "failed"
    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["extended"] is False


async def test_a_cancelled_relay_leaves_the_turn_alone_and_says_so(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture
) -> None:
    runner_drop_state.note(_RUNNER_ID, "silent")
    task = relay_case.start()
    await asyncio.wait_for(relay_case.transport.extension_waiting.wait(), budget(5.0))

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert relay_case.status() == "running"
    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["outcome"] == "superseded"
    assert grace.attributes["extended"] is True


async def test_a_sub_agent_relay_resolves_the_host_of_its_parent(
    relay_case: _RelayCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child shares its parent's runner and has no host of its own."""
    child = relay_case.store.create_conversation(
        kind="sub_agent", parent_conversation_id=relay_case.session_id, runner_id=_RUNNER_ID
    )
    assert child.host_id is None
    assert await orchestration._relay_host_id(child.id, relay_case.store) == _HOST_ID
    assert await orchestration._relay_host_id(relay_case.session_id, relay_case.store) == _HOST_ID


async def test_an_unreadable_or_unbound_session_has_no_host(relay_case: _RelayCase) -> None:
    unbound = relay_case.store.create_conversation()
    assert await orchestration._relay_host_id(unbound.id, relay_case.store) is None
    assert await orchestration._relay_host_id("missing-session", relay_case.store) is None

    class _BrokenStore:
        def get_conversation(self, _session_id: str) -> None:
            raise RuntimeError("database unavailable")

    assert await orchestration._relay_host_id(relay_case.session_id, _BrokenStore()) is None  # type: ignore[arg-type]
