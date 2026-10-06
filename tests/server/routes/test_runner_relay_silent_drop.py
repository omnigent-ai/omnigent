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
from omnigent.server.routes._sessions.common import _ACP_SUBAGENT_ID_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.budgets import budget

_RUNNER_ID = "runner-relay-silent-drop"
_HOST_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
_NORMAL_GRACE_S = 0.1
_READY_FRAME = 'data: {"type":"session.heartbeat"}\n\n'
_DONE_STREAM = (
    _READY_FRAME + 'data: {"type":"session.status","status":"idle"}\n\n' + "data: [DONE]\n\n"
)


class _TunnelLikeTransport(httpx.AsyncBaseTransport):
    """Models ``WSTunnelTransport``: refuses requests until the runner registers.

    The first request is the relay attaching: it gets the runner's ready heartbeat and the
    stream then ends, as when a tunnel drops under an attached relay. Later requests follow
    the runner's registration.

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

    def go_offline(self) -> None:
        self.online = False
        self._registered.clear()

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
        if self.attempts == 1:
            # The relay attaches: the ready heartbeat, then the stream ends.
            return httpx.Response(200, text=_READY_FRAME)
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

    def start(self, session_id: str | None = None) -> asyncio.Task[None]:
        return asyncio.create_task(
            orchestration._relay_runner_stream(
                session_id or self.session_id, self.client, self.store, runner_id=_RUNNER_ID
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
    """A mid-turn, host-bound session with short graces and an offline host.

    Its relay attaches on the first request, then the tunnel drops.
    """
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", _NORMAL_GRACE_S)
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_GRACE_S", 0.8)
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_RECHECK_S", 0.05)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.02)
    host_online = threading.Event()
    monkeypatch.setattr(
        runner_drop_state, "_host_online_probe", lambda _host_id: host_online.is_set()
    )
    monkeypatch.setattr(runner_drop_state, "_host_managed_probe", None)
    monkeypatch.setattr(runner_drop_state, "_PROBE_TTL_S", 0.0)
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


async def test_a_second_silent_drop_after_the_stream_resumes_is_held_again(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream that reaches ready confirms recovery; the next silent drop earns its own hold."""
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_GRACE_S", 60.0)
    transport = relay_case.transport
    resumed = asyncio.Event()

    def ready_then_quiet_again(_request: httpx.Request) -> httpx.Response:
        # The resumed stream reaches ready; then the laptop goes quiet again mid-turn.
        transport.respond = lambda _request: httpx.Response(200, text=_DONE_STREAM)
        runner_drop_state.note(_RUNNER_ID, "silent")
        transport.go_offline()
        resumed.set()
        return httpx.Response(200, text=_READY_FRAME)

    transport.respond = ready_then_quiet_again
    runner_drop_state.note(_RUNNER_ID, "silent")
    task = relay_case.start()
    await asyncio.wait_for(transport.extension_started.wait(), budget(5.0))
    runner_drop_state.clear(_RUNNER_ID)
    transport.register()
    await asyncio.wait_for(resumed.wait(), budget(5.0))

    async def _second_hold_waiting() -> None:
        # The second hold opens with its own zero-length check, then parks on the runner.
        while transport.waits.count(0.0) < 2:
            await asyncio.sleep(0.01)
        parked_after = len(transport.waits)
        while len(transport.waits) <= parked_after:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_second_hold_waiting(), budget(5.0))
    assert not task.done()
    assert relay_case.status() == "running", "the second drop must not fail the turn"

    runner_drop_state.clear(_RUNNER_ID)
    transport.register()
    await asyncio.wait_for(task, budget(5.0))

    assert relay_case.status() == "idle"
    assert relay_case.last_error_code() is None
    assert not _rows(caplog, "session_turn_failed")
    first, second = _rows(caplog, "runner_disconnect_grace")
    for grace in (first, second):
        assert grace.attributes["outcome"] == "reconnected"
        assert grace.attributes["extended"] is True
        assert grace.attributes["drop_kind"] == "silent"


@pytest.mark.parametrize("kind", ["silent", "sudden"])
async def test_waited_time_is_measured_from_the_recorded_drop(
    relay_case: _RelayCase,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    kind: runner_drop_state.DropKind,
) -> None:
    """The tunnel ended before the relay noticed; the clock starts at the drop, as the timer's."""
    age_s = 0.5
    monkeypatch.setitem(
        runner_drop_state._drops,
        _RUNNER_ID,
        runner_drop_state.RunnerDrop(kind=kind, dropped_at=time.monotonic() - age_s),
    )

    await asyncio.wait_for(relay_case.start(), budget(5.0))

    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["extended"] is (kind == "silent")
    # A silent drop is held to the end of its 0.8 s grace; a sudden one to the normal 0.1 s.
    assert grace.attributes["waited_s"] >= (0.8 if kind == "silent" else age_s + _NORMAL_GRACE_S)


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


async def test_silent_drop_with_the_host_already_online_gets_one_recheck(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that woke a moment ago gives its runner one more recheck, then the give-up."""
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_GRACE_S", 60.0)
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_RECHECK_S", 0.2)
    relay_case.host_online.set()
    runner_drop_state.note(_RUNNER_ID, "silent")
    task = relay_case.start()
    await asyncio.wait_for(relay_case.transport.extension_waiting.wait(), budget(5.0))
    assert not task.done(), "the host being online must not end the wait at once"

    await asyncio.wait_for(task, budget(5.0))

    assert relay_case.status() == "failed"
    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["outcome"] == "host_back_runner_missing"
    assert grace.attributes["extended"] is True
    assert grace.attributes["host_online"] is True
    assert grace.attributes["drop_kind"] == "silent"
    assert 0.2 in relay_case.transport.waits, "the runner was given a full recheck interval"


async def test_a_runner_following_an_already_online_host_resumes_the_stream(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_GRACE_S", 60.0)
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_RECHECK_S", 5.0)
    relay_case.host_online.set()
    runner_drop_state.note(_RUNNER_ID, "silent")
    task = relay_case.start()
    await asyncio.wait_for(relay_case.transport.extension_waiting.wait(), budget(5.0))

    runner_drop_state.clear(_RUNNER_ID)
    relay_case.transport.register()
    await asyncio.wait_for(task, budget(5.0))

    assert relay_case.status() == "idle"
    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["outcome"] == "reconnected"
    assert grace.attributes["host_online"] is True


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
    requests = 0

    def attach_then_refuse(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, text=_READY_FRAME)
        raise httpx.ConnectError("runner is offline", request=request)

    runner_drop_state.note(_RUNNER_ID, "silent")
    async with httpx.AsyncClient(
        base_url="http://runner", transport=httpx.MockTransport(attach_then_refuse)
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


async def test_an_unbound_session_has_no_host_and_an_unreadable_one_raises(
    relay_case: _RelayCase,
) -> None:
    unbound = relay_case.store.create_conversation()
    assert await orchestration._relay_host_id(unbound.id, relay_case.store) is None
    assert (
        await orchestration._relay_host_id("0123456789abcdef0123456789abcdef", relay_case.store)
        is None
    )

    class _BrokenStore:
        def get_conversation(self, _session_id: str) -> None:
            raise RuntimeError("database unavailable")

    # The hold treats a failed lookup as an unresolved host and retries it at each recheck.
    with pytest.raises(RuntimeError):
        await orchestration._relay_host_id(relay_case.session_id, _BrokenStore())  # type: ignore[arg-type]


# ── what is worth holding for ───────────────────────────────────────────────


async def test_an_idle_session_is_not_held(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture
) -> None:
    orchestration._session_status_cache[relay_case.session_id] = "idle"
    runner_drop_state.note(_RUNNER_ID, "silent")

    await asyncio.wait_for(relay_case.start(), budget(5.0))

    assert relay_case.status() == "idle"
    assert relay_case.last_error_code() is None
    assert relay_case.transport.extension_started.is_set(), "the drop was weighed"
    assert not relay_case.transport.extension_waiting.is_set(), "but never waited on"
    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["extended"] is False
    (gave_up,) = _rows(caplog, "runner_stream_disconnected")
    assert gave_up.attributes["decision"] == "idle_no_failure"


async def test_a_cold_cache_is_read_from_the_saved_row(relay_case: _RelayCase) -> None:
    """Without a live edge the relay holds on what the disconnect decision would read."""
    orchestration._session_status_cache.pop(relay_case.session_id, None)
    relay_case.store.set_session_live_status(relay_case.session_id, "running")
    runner_drop_state.note(_RUNNER_ID, "silent")
    task = relay_case.start()
    await asyncio.wait_for(relay_case.transport.extension_waiting.wait(), budget(5.0))
    assert not task.done()

    runner_drop_state.clear(_RUNNER_ID)
    relay_case.transport.register()
    await asyncio.wait_for(task, budget(5.0))
    assert relay_case.last_error_code() is None


async def test_a_parent_owned_mirror_is_not_held(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture
) -> None:
    """A native parent's runtime drives a mirrored sub-agent's turn, so nothing is lost here."""
    mirror = relay_case.store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=relay_case.session_id,
        runner_id=_RUNNER_ID,
        labels={_ACP_SUBAGENT_ID_LABEL_KEY: "acp-subagent-1"},
    )
    relay_case.store.set_session_live_status(mirror.id, "running")
    runner_drop_state.note(_RUNNER_ID, "silent")
    try:
        await asyncio.wait_for(relay_case.start(mirror.id), budget(5.0))
    finally:
        orchestration._session_status_cache.pop(mirror.id, None)

    assert relay_case.transport.extension_started.is_set()
    assert not relay_case.transport.extension_waiting.is_set()
    (decision,) = _rows(caplog, "runner_disconnect_decision")
    assert decision.attributes["decision"] == "subagent_unobserved"
    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["extended"] is False
    assert not _rows(caplog, "session_turn_failed")


async def test_a_managed_sandbox_host_is_not_held(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sandbox cannot wake on its own, so a silent drop there keeps the normal grace."""
    monkeypatch.setattr(
        runner_drop_state, "_host_managed_probe", lambda host_id: host_id == _HOST_ID
    )
    runner_drop_state.note(_RUNNER_ID, "silent")

    await asyncio.wait_for(relay_case.start(), budget(5.0))

    assert relay_case.status() == "failed"
    assert not relay_case.transport.extension_waiting.is_set()
    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["extended"] is False
    assert grace.attributes["drop_kind"] == "silent"


async def test_a_zero_silent_grace_turns_the_hold_off(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_GRACE_S", 0.0)
    runner_drop_state.note(_RUNNER_ID, "silent")

    await asyncio.wait_for(relay_case.start(), budget(5.0))

    assert relay_case.status() == "failed"
    assert 0.0 not in relay_case.transport.waits, "nothing about the silent drop is consulted"
    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["extended"] is False
    assert grace.attributes["grace_s"] == _NORMAL_GRACE_S


async def test_a_runner_live_on_another_replica_ends_the_hold_without_failing_the_turn(
    relay_case: _RelayCase, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestration, "RUNNER_SILENT_DROP_GRACE_S", 60.0)
    runner_drop_state.note(_RUNNER_ID, "silent")
    task = relay_case.start()
    await asyncio.wait_for(relay_case.transport.extension_waiting.wait(), budget(5.0))
    assert not task.done()

    # The runner re-tunnels to another replica, which stamps the shared row.
    relay_case.store.touch_runner_liveness([_RUNNER_ID], int(time.time()) + 5)
    await asyncio.wait_for(task, budget(5.0))

    (grace,) = _rows(caplog, "runner_disconnect_grace")
    assert grace.attributes["outcome"] == "live_elsewhere"
    assert grace.attributes["extended"] is True
    (gave_up,) = _rows(caplog, "runner_stream_disconnected")
    assert gave_up.attributes["decision"] == "live_elsewhere"
    assert not _rows(caplog, "session_turn_failed")
    assert relay_case.last_error_code() is None
    assert relay_case.status() is None, "this replica let go of the session"


# ── configuration ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 900.0),
        ("", 900.0),
        ("   ", 900.0),
        ("120", 120.0),
        ("45.5", 45.5),
        ("0", 0.0),
        ("-30", 0.0),
        ("soon", 900.0),
        ("nan", 900.0),
        ("inf", 900.0),
    ],
)
def test_silent_drop_grace_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float
) -> None:
    name = "OMNIGENT_RUNNER_SILENT_DROP_GRACE_S"
    if raw is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, raw)
    assert orchestration._silent_drop_grace_from_env() == expected
