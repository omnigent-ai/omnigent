"""Runner-scoped stop intent, rollback, and sessions without a local relay."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.host.frames import HostHelloFrame, HostStopRunnerFrame, decode_host_frame
from omnigent.runtime import session_stream
from omnigent.server import session_live_state
from omnigent.server.host_registry import HostRegistry, WebSocketLike
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import common, helpers, orchestration
from omnigent.server.schemas import ErrorDetail
from omnigent.stores.conversation_store import RUNNER_LIVENESS_TTL_S
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_RUNNER = "runner-stopped"


@pytest.fixture
def family(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[SqlAlchemyConversationStore, dict[str, str]]]:
    monkeypatch.setattr(orchestration, "_RUNNER_STOP_STATUS_BATCH_SIZE", 2)
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    ids = {"parent": parent.id}
    store.set_runner_id(parent.id, _RUNNER)
    for name, status in (
        ("active", "running"),
        ("cold", "waiting"),
        ("finished", "idle"),
        ("failed", "failed"),
        ("elsewhere", "running"),
        ("grandchild", "running"),
    ):
        row = store.create_conversation(
            kind="sub_agent",
            parent_conversation_id=ids["active"] if name == "grandchild" else parent.id,
        )
        ids[name] = row.id
        store.set_runner_id(row.id, "runner-other" if name == "elsewhere" else _RUNNER)
        store.set_session_live_status(row.id, status)
        if name != "cold":
            sessions._session_status_cache[row.id] = status
    store.set_labels(ids["failed"], {"omnigent.last_task_error_code": "native_turn_error"})
    try:
        yield store, ids
    finally:
        for session_id in ids.values():
            sessions._intentional_stop_sessions.pop(session_id, None)
            sessions._session_status_cache.pop(session_id, None)
            sessions._runner_relay_tasks.pop(session_id, None)
            session_stream.close(session_id)


@pytest.mark.parametrize("outcome", ["delivered", "offline", "error", "cancelled"])
async def test_stop_marks_only_affected_active_sessions_and_rolls_back(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    store, ids = family
    expected = {ids[name] for name in ("parent", "active", "cold", "grandchild")}

    async def teardown(*_args, **_kwargs):
        assert set(sessions._intentional_stop_sessions) == expected
        if outcome == "error":
            raise RuntimeError("host stop failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return outcome == "delivered"

    monkeypatch.setattr(sessions, "_stop_session_host_runner", teardown)
    call = orchestration._stop_host_runner_intentionally(
        ids["parent"], "host", _RUNNER, None, store
    )
    if outcome == "error":
        with pytest.raises(RuntimeError, match="host stop failed"):
            await call
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        assert await call is (outcome == "delivered")
    assert set(sessions._intentional_stop_sessions) == (
        expected if outcome == "delivered" else set()
    )
    assert (
        store.get_conversation(ids["failed"]).labels["omnigent.last_task_error_code"]
        == "native_turn_error"
    )
    assert sessions._session_status_cache[ids["elsewhere"]] == "running"


@pytest.mark.parametrize("outcome", ["cancelled", "timeout", "rejected", "replaced", "stopped"])
async def test_stop_intent_tracks_actual_host_frame_handoff(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    store, ids = family
    registry = HostRegistry()
    conn = registry.register(
        "host",
        AsyncMock(spec=WebSocketLike),
        HostHelloFrame(version="0.1.0", frame_protocol_version=1, name="host"),
        owner=None,
    )
    if outcome == "timeout":
        monkeypatch.setattr(helpers, "_STOP_RUNNER_RESULT_TIMEOUT_S", 0.1)
    if outcome == "replaced":
        send = registry.send_text

        def reject_stale_connection(connection, frame):
            registry.deregister("host")
            send(connection, frame)

        monkeypatch.setattr(registry, "send_text", reject_stale_connection)
    stop = asyncio.create_task(
        orchestration._stop_host_runner_intentionally(
            ids["parent"], "host", _RUNNER, registry, store
        )
    )
    try:
        encoded = await asyncio.wait_for(conn.outbound_queue.get(), timeout=10)
        if outcome == "replaced":
            assert encoded is None
        else:
            assert encoded is not None
            frame = decode_host_frame(encoded)
            assert isinstance(frame, HostStopRunnerFrame)
            assert frame.runner_id == _RUNNER
            if outcome == "cancelled":
                stop.cancel()
            elif outcome in {"rejected", "stopped"}:
                conn.pending_stops.pop(frame.request_id).set_result(
                    {"status": "failed" if outcome == "rejected" else "stopped"}
                )
        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await stop
        else:
            assert await asyncio.wait_for(stop, timeout=10) is (outcome == "stopped")
        expected_stop = outcome in {"cancelled", "timeout", "stopped"}
        expected_markers = {ids[name] for name in ("parent", "active", "cold", "grandchild")}
        assert set(sessions._intentional_stop_sessions) == (
            expected_markers if expected_stop else set()
        )
        assert not conn.pending_stops
        error = ErrorDetail(code="runner_disconnected", message="Runner disappeared.")
        await sessions._mark_runner_sessions_offline(
            store.list_conversations_by_runner_id(_RUNNER), error, store
        )
        for name in ("active", "cold", "grandchild"):
            child_id = ids[name]
            assert sessions._session_status_cache[child_id] == (
                "idle" if expected_stop else "failed"
            )
            last_error = sessions._last_task_error_from_labels(
                store.get_conversation(child_id).labels
            )
            if expected_stop:
                assert last_error is None
            else:
                assert last_error["code"] == "runner_disconnected"
    finally:
        stop.cancel()
        await asyncio.gather(stop, return_exceptions=True)
        registry.deregister("host")


async def test_disconnect_sweep_settles_children_without_relays_and_consumes_intent(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ids = family

    async def teardown(*_args, **_kwargs):
        return True

    monkeypatch.setattr(sessions, "_stop_session_host_runner", teardown)
    assert await orchestration._stop_host_runner_intentionally(
        ids["parent"], "host", _RUNNER, None, store
    )
    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")
    await sessions._mark_runner_sessions_offline(
        store.list_conversations_by_runner_id(_RUNNER), error, store
    )
    for name in ("active", "cold", "grandchild"):
        assert sessions._session_status_cache[ids[name]] == "idle"
        assert (
            sessions._last_task_error_from_labels(store.get_conversation(ids[name]).labels) is None
        )
    assert not set(sessions._intentional_stop_sessions)
    assert sessions._session_status_cache[ids["finished"]] == "idle"
    assert sessions._session_status_cache[ids["failed"]] == "failed"

    # A later active turn losing its runner must still report the genuine failure.
    sessions._session_status_cache[ids["active"]] = "running"
    await sessions._mark_runner_sessions_offline(
        [store.get_conversation(ids["active"])], error, store
    )
    assert sessions._session_status_cache[ids["active"]] == "failed"
    assert (
        sessions._last_task_error_from_labels(store.get_conversation(ids["active"]).labels)["code"]
        == "runner_disconnected"
    )


async def test_sweep_leaves_intent_for_a_matching_live_relay(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
) -> None:
    store, ids = family
    child_id = ids["active"]
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    sessions._runner_relay_tasks[child_id] = sessions._RelayHandle(_RUNNER, task, gate)
    sessions._intentional_stop_sessions[child_id] = _RUNNER
    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")
    try:
        await sessions._mark_runner_sessions_offline(
            [store.get_conversation(child_id)], error, store
        )
        assert sessions._intentional_stop_sessions.get(child_id) == _RUNNER
        assert sessions._session_status_cache[child_id] == "running"
        assert (
            sessions._last_task_error_from_labels(store.get_conversation(child_id).labels) is None
        )
        gate.set()
        await task
        await sessions._mark_runner_sessions_offline(
            [store.get_conversation(child_id)], error, store
        )
        assert child_id not in sessions._intentional_stop_sessions
        assert sessions._session_status_cache[child_id] == "idle"
    finally:
        gate.set()
        await task


@pytest.mark.parametrize("delivered", [True, False])
@pytest.mark.parametrize("first_page_succeeds", [False, True])
async def test_stop_uses_live_relay_binding_when_row_lookup_fails(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    delivered: bool,
    first_page_succeeds: bool,
) -> None:
    store, ids = family
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    sessions._runner_relay_tasks[ids["cold"]] = sessions._RelayHandle(_RUNNER, task, gate)
    sessions._runner_relay_tasks[ids["elsewhere"]] = sessions._RelayHandle(
        "runner-other", task, gate
    )

    first_page = sorted([(ids["active"], "running"), (ids["finished"], "idle")])
    cursors: list[str | None] = []
    expected = {ids["parent"], ids["cold"]}
    if first_page_succeeds:
        expected.add(ids["active"])

    def unavailable(_runner_id, *, after=None, **_kwargs):
        cursors.append(after)
        if first_page_succeeds and after is None:
            return first_page
        raise RuntimeError("store temporarily unavailable")

    async def teardown(*_args, **_kwargs):
        assert set(sessions._intentional_stop_sessions) == expected
        return delivered

    monkeypatch.setattr(store, "list_runner_session_statuses", unavailable)
    monkeypatch.setattr(sessions, "_stop_session_host_runner", teardown)
    try:
        result = await orchestration._stop_host_runner_intentionally(
            ids["parent"], "host", _RUNNER, None, store
        )
        assert result is delivered
        assert cursors == ([None, first_page[-1][0]] if first_page_succeeds else [None])
        assert set(sessions._intentional_stop_sessions) == (expected if delivered else set())
    finally:
        gate.set()
        await task


async def test_child_turn_started_during_teardown_remains_a_reported_failure(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New child work is outside the stop's snapshot of already active turns."""
    store, ids = family
    entered, release = asyncio.Event(), asyncio.Event()

    async def teardown(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(sessions, "_stop_session_host_runner", teardown)
    stop = asyncio.create_task(
        orchestration._stop_host_runner_intentionally(ids["parent"], "host", _RUNNER, None, store)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        child_id = ids["finished"]
        sessions._session_status_cache[child_id] = "running"
        release.set()
        assert await asyncio.wait_for(stop, timeout=10)
        error = ErrorDetail(
            code="runner_disconnected", message="Runner disconnected unexpectedly."
        )
        await sessions._mark_runner_sessions_offline(
            store.list_conversations_by_runner_id(_RUNNER), error, store
        )
        assert sessions._session_status_cache[ids["active"]] == "idle"
        assert sessions._session_status_cache[child_id] == "failed"
        persisted = sessions._last_task_error_from_labels(store.get_conversation(child_id).labels)
        assert persisted is not None and persisted["code"] == "runner_disconnected"
    finally:
        release.set()
        stop.cancel()
        await asyncio.gather(stop, return_exceptions=True)


async def test_stop_burst_does_not_evict_other_pending_stops(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending stop intent must survive large concurrent bursts."""
    store, ids = family
    pending = {f"pending-stop-{i}": "runner-previous-stop" for i in range(16384)}
    sessions._intentional_stop_sessions.update(pending)
    expected = pending | {
        ids[name]: _RUNNER for name in ("parent", "active", "cold", "grandchild")
    }

    async def teardown(*_args, **_kwargs):
        assert dict(sessions._intentional_stop_sessions.items()) == expected
        return True

    monkeypatch.setattr(sessions, "_stop_session_host_runner", teardown)
    try:
        assert await orchestration._stop_host_runner_intentionally(
            ids["parent"], "host", _RUNNER, None, store
        )
        assert dict(sessions._intentional_stop_sessions.items()) == expected
    finally:
        for session_id in pending:
            sessions._intentional_stop_sessions.pop(session_id, None)


async def test_unconsumed_stop_expires_before_a_later_disconnect(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ids = family
    now = time.monotonic()
    monkeypatch.setattr(common, "time", SimpleNamespace(monotonic=lambda: now))

    async def teardown(*_args, **_kwargs):
        return True

    monkeypatch.setattr(sessions, "_stop_session_host_runner", teardown)
    assert await orchestration._stop_host_runner_intentionally(
        ids["parent"], "host", _RUNNER, None, store
    )
    # No local relay or sweep consumes these markers after the runner moves away.
    now += RUNNER_LIVENESS_TTL_S + 1
    assert sessions._intentional_stop_sessions.get(ids["cold"]) == _RUNNER
    now += RUNNER_LIVENESS_TTL_S
    assert ids["cold"] not in sessions._intentional_stop_sessions

    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")
    await sessions._mark_runner_sessions_offline(
        [store.get_conversation(ids["cold"])], error, store
    )
    assert sessions._session_status_cache[ids["cold"]] == "failed"
    assert (
        sessions._last_task_error_from_labels(store.get_conversation(ids["cold"]).labels)["code"]
        == "runner_disconnected"
    )


@pytest.mark.parametrize("outcome", ["stopped", "timeout"])
async def test_repeated_stop_renews_retained_intent(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    store, ids = family
    now = time.monotonic()
    monkeypatch.setattr(common, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(helpers, "_STOP_RUNNER_RESULT_TIMEOUT_S", 0.1)
    registry = HostRegistry()
    conn = registry.register(
        "host",
        AsyncMock(spec=WebSocketLike),
        HostHelloFrame(version="0.1.0", frame_protocol_version=1, name="host"),
        owner=None,
    )
    first = asyncio.create_task(
        orchestration._stop_host_runner_intentionally(
            ids["parent"], "host", _RUNNER, registry, store
        )
    )
    second: asyncio.Task[bool] | None = None
    try:
        encoded = await asyncio.wait_for(conn.outbound_queue.get(), timeout=10)
        assert encoded is not None
        frame = decode_host_frame(encoded)
        assert isinstance(frame, HostStopRunnerFrame)
        assert frame.runner_id == _RUNNER
        assert not await asyncio.wait_for(first, timeout=10)
        assert not conn.pending_stops

        now += 2 * RUNNER_LIVENESS_TTL_S - 1
        if outcome == "stopped":
            monkeypatch.setattr(helpers, "_STOP_RUNNER_RESULT_TIMEOUT_S", 10.0)
        second = asyncio.create_task(
            orchestration._stop_host_runner_intentionally(
                ids["parent"], "host", _RUNNER, registry, store
            )
        )
        encoded = await asyncio.wait_for(conn.outbound_queue.get(), timeout=10)
        assert encoded is not None
        frame = decode_host_frame(encoded)
        assert isinstance(frame, HostStopRunnerFrame)
        assert frame.runner_id == _RUNNER
        now += 2
        if outcome == "stopped":
            conn.pending_stops.pop(frame.request_id).set_result({"status": "stopped"})
        assert await asyncio.wait_for(second, timeout=10) is (outcome == "stopped")
        assert not conn.pending_stops

        error = ErrorDetail(code="runner_disconnected", message="Runner disappeared.")
        await sessions._mark_runner_sessions_offline(
            store.list_conversations_by_runner_id(_RUNNER), error, store
        )
        for name in ("active", "cold", "grandchild"):
            child_id = ids[name]
            assert sessions._session_status_cache[child_id] == "idle"
            assert (
                sessions._last_task_error_from_labels(store.get_conversation(child_id).labels)
                is None
            )
    finally:
        pending = [first] if second is None else [first, second]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        registry.deregister("host")


@pytest.mark.parametrize("stopped_runner", [_RUNNER, "runner-replacement"])
async def test_stop_arriving_during_status_lookup_matches_the_departed_runner(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    stopped_runner: str,
) -> None:
    store, ids = family
    child_id = ids["cold"]
    snapshot = store.get_conversation(child_id)
    read = store.get_conversation
    entered, release = threading.Event(), threading.Event()

    def blocked_read(session_id: str):
        entered.set()
        assert release.wait(timeout=10), "test did not release the status lookup"
        return read(session_id)

    monkeypatch.setattr(store, "get_conversation", blocked_read)
    task = asyncio.create_task(
        orchestration._runner_disconnect_requires_failure(
            child_id, store, origin="runner_offline_sweep", snapshot=snapshot
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        sessions._intentional_stop_sessions[child_id] = stopped_runner
        release.set()
        assert await asyncio.wait_for(task, timeout=10) is (stopped_runner != _RUNNER)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=10)


@pytest.mark.parametrize("rebound", [False, True])
async def test_binding_lookup_failure_does_not_abort_offline_sweep(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    rebound: bool,
) -> None:
    store, ids = family
    child_id = ids["active"]
    snapshot = store.get_conversation(child_id)
    if rebound:
        store.replace_runner_id(child_id, "runner-replacement")
    sessions._intentional_stop_sessions[child_id] = "runner-replacement"
    sessions._intentional_stop_sessions[ids["cold"]] = _RUNNER
    lookup = store.get_runner_liveness
    attempts = 0

    def unavailable(_session_id):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("runner binding lookup unavailable")

    monkeypatch.setattr(store, "get_runner_liveness", unavailable)
    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")
    await sessions._mark_runner_sessions_offline(
        [snapshot, store.get_conversation(ids["cold"]), store.get_conversation(ids["grandchild"])],
        error,
        store,
    )
    assert attempts == 3
    assert sessions._intentional_stop_sessions[child_id] == "runner-replacement"
    assert sessions._session_status_cache[child_id] == "running"
    assert store.get_conversation(child_id).live_status == "running"
    assert sessions._session_status_cache[ids["cold"]] == "idle"
    assert ids["cold"] not in sessions._intentional_stop_sessions
    assert sessions._session_status_cache[ids["grandchild"]] == "failed"
    assert (
        sessions._last_task_error_from_labels(store.get_conversation(ids["grandchild"]).labels)[
            "code"
        ]
        == "runner_disconnected"
    )

    monkeypatch.setattr(store, "get_runner_liveness", lookup)
    await sessions._mark_runner_sessions_offline([snapshot], error, store)
    if rebound:
        assert sessions._session_status_cache[child_id] == "running"
        assert sessions._intentional_stop_sessions[child_id] == "runner-replacement"
    else:
        assert sessions._session_status_cache[child_id] == "failed"
        assert child_id not in sessions._intentional_stop_sessions


@pytest.mark.parametrize("stopped_runner", [_RUNNER, "runner-replacement"])
@pytest.mark.parametrize("with_relay", [False, True])
async def test_old_runner_sweep_preserves_rebound_session(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    stopped_runner: str,
    with_relay: bool,
) -> None:
    store, ids = family
    child_id = ids["active"]
    old_row = store.get_conversation(child_id)
    store.replace_runner_id(child_id, "runner-replacement")
    replacement_error = {"omnigent.last_task_error_code": "replacement_error"}
    store.set_labels(child_id, replacement_error)
    sessions._intentional_stop_sessions[child_id] = stopped_runner
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    if with_relay:
        sessions._runner_relay_tasks[child_id] = sessions._RelayHandle(
            "runner-replacement", task, gate
        )
    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")
    try:
        await sessions._mark_runner_sessions_offline([old_row], error, store)
        assert sessions._session_status_cache[child_id] == "running"
        after = store.get_conversation(child_id)
        assert after.runner_id == "runner-replacement"
        assert after.live_status == "running"
        assert after.labels["omnigent.last_task_error_code"] == "replacement_error"
        if stopped_runner == _RUNNER:
            assert child_id not in sessions._intentional_stop_sessions
            # The old stop must not suppress a real crash of the replacement.
            await sessions._mark_runner_sessions_offline(
                [store.get_conversation(child_id)], error, store
            )
            assert sessions._session_status_cache[child_id] == "failed"
        else:
            assert sessions._intentional_stop_sessions.get(child_id) == "runner-replacement"
    finally:
        gate.set()
        await task


@pytest.mark.parametrize("rebound", [False, True])
async def test_offline_sweep_retries_a_transient_binding_read(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    rebound: bool,
) -> None:
    store, ids = family
    child_id = ids["active"]
    snapshot = store.get_conversation(child_id)
    if rebound:
        store.replace_runner_id(child_id, "runner-replacement")
    sessions._intentional_stop_sessions[child_id] = "runner-replacement"
    sessions._intentional_stop_sessions[ids["cold"]] = _RUNNER
    lookup = store.get_runner_liveness
    attempts = 0

    def transient_failure(session_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("runner binding temporarily unavailable")
        assert sessions._session_status_cache[ids["cold"]] == "idle"
        assert sessions._session_status_cache[ids["grandchild"]] == "failed"
        return lookup(session_id)

    monkeypatch.setattr(store, "get_runner_liveness", transient_failure)
    error = ErrorDetail(code="runner_disconnected", message="Runner disappeared.")
    await sessions._mark_runner_sessions_offline(
        [snapshot, store.get_conversation(ids["cold"]), store.get_conversation(ids["grandchild"])],
        error,
        store,
    )
    assert attempts == 2
    if rebound:
        assert sessions._session_status_cache[child_id] == "running"
        assert sessions._intentional_stop_sessions[child_id] == "runner-replacement"
        assert not sessions._last_task_error_from_labels(store.get_conversation(child_id).labels)
    else:
        assert sessions._session_status_cache[child_id] == "failed"
        assert child_id not in sessions._intentional_stop_sessions
        assert (
            sessions._last_task_error_from_labels(store.get_conversation(child_id).labels)["code"]
            == "runner_disconnected"
        )


@pytest.mark.parametrize("recovery", ["idle", "failed", "rebound", "relay"])
async def test_offline_sweep_retries_a_transient_settlement_write(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    recovery: str,
) -> None:
    store, ids = family
    child_id = ids["active"]
    snapshot = store.get_conversation(child_id)
    settle = store.settle_intentionally_stopped_session
    failed = threading.Event()
    sessions._intentional_stop_sessions[child_id] = _RUNNER

    def transient_failure(*args):
        if not failed.is_set():
            failed.set()
            raise RuntimeError("metadata write temporarily unavailable")
        return settle(*args)

    monkeypatch.setattr(store, "settle_intentionally_stopped_session", transient_failure)
    error = ErrorDetail(code="runner_disconnected", message="Runner disappeared.")
    sweep = asyncio.create_task(sessions._mark_runner_sessions_offline([snapshot], error, store))
    relay = None
    relay_gate = asyncio.Event()
    try:
        assert await asyncio.to_thread(failed.wait, 10)
        if recovery == "failed":
            store.set_session_live_status(child_id, "failed")
            sessions._session_status_cache[child_id] = "failed"
        elif recovery == "rebound":
            store.replace_runner_id(child_id, "runner-replacement")
        elif recovery == "relay":
            relay = asyncio.create_task(relay_gate.wait())
            sessions._runner_relay_tasks[child_id] = sessions._RelayHandle(
                _RUNNER, relay, relay_gate
            )
        if recovery in {"failed", "rebound"}:
            store.set_labels(child_id, {"omnigent.last_task_error_code": "preserved"})
        await asyncio.wait_for(sweep, timeout=10)
        after = store.get_conversation(child_id)
        expected = "running" if recovery in {"rebound", "relay"} else recovery
        assert after.live_status == expected
        assert sessions._session_status_cache[child_id] == expected
        if recovery == "relay":
            assert sessions._intentional_stop_sessions[child_id] == _RUNNER
        else:
            assert child_id not in sessions._intentional_stop_sessions
        if recovery in {"failed", "rebound"}:
            assert after.labels["omnigent.last_task_error_code"] == "preserved"
        if recovery == "rebound":
            assert after.runner_id == "runner-replacement"
    finally:
        relay_gate.set()
        sweep.cancel()
        await asyncio.wait_for(asyncio.gather(sweep, return_exceptions=True), timeout=10)
        if relay is not None:
            await asyncio.wait_for(relay, timeout=10)


@pytest.mark.parametrize("handoff", ["unchanged", "rebind", "relay"])
async def test_stop_settlement_is_ordered_and_checks_the_current_binding(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    handoff: str,
) -> None:
    store, ids = family
    child_id = ids["active"]
    old_row = store.get_conversation(child_id)
    entered, release = threading.Event(), threading.Event()
    write_status = store.set_session_live_status

    def blocked_write(session_id, status):
        entered.set()
        assert release.wait(timeout=10), "test did not release the pending status write"
        write_status(session_id, status)

    monkeypatch.setattr(store, "set_session_live_status", blocked_write)
    session_live_state.configure(store)
    sessions._intentional_stop_sessions[child_id] = _RUNNER
    session_live_state.persist_live_status(child_id, "running")
    sweep = None
    relay = None
    relay_gate = asyncio.Event()
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        error = ErrorDetail(code="runner_disconnected", message="Runner disappeared.")
        sweep = asyncio.create_task(
            sessions._mark_runner_sessions_offline([old_row], error, store)
        )
        await asyncio.wait({sweep}, timeout=0.1)
        assert not sweep.done(), "Settlement must follow the pending status write"
        if handoff == "rebind":
            store.replace_runner_id(child_id, "runner-replacement")
            store.set_labels(child_id, {"omnigent.last_task_error_code": "replacement_error"})
        elif handoff == "relay":
            relay = asyncio.create_task(relay_gate.wait())
            sessions._runner_relay_tasks[child_id] = sessions._RelayHandle(
                _RUNNER, relay, relay_gate
            )
        release.set()
        await asyncio.wait_for(sweep, timeout=10)
        if relay is not None:
            assert sessions._session_status_cache[child_id] == "running"
            assert sessions._intentional_stop_sessions.get(child_id) == _RUNNER
            relay_gate.set()
            await asyncio.wait_for(relay, timeout=10)
            await sessions._mark_runner_sessions_offline([old_row], error, store)
        after = store.get_conversation(child_id)
        assert after.live_status == ("running" if handoff == "rebind" else "idle")
        assert sessions._session_status_cache[child_id] == after.live_status
        if handoff == "rebind":
            assert after.labels["omnigent.last_task_error_code"] == "replacement_error"
        else:
            # Settlement must not deduplicate away the next real running edge.
            session_live_state.persist_live_status(child_id, "running")
            await asyncio.wait_for(
                asyncio.wrap_future(session_live_state.submit("drain_test_writes", lambda: None)),
                timeout=10,
            )
            assert store.get_conversation(child_id).live_status == "running"
    finally:
        release.set()
        relay_gate.set()
        if relay is not None:
            await asyncio.wait_for(relay, timeout=10)
        if sweep is not None:
            await asyncio.wait_for(asyncio.gather(sweep, return_exceptions=True), timeout=10)
        await asyncio.wait_for(
            asyncio.wrap_future(session_live_state.submit("drain_test_writes", lambda: None)),
            timeout=10,
        )
        session_live_state.configure(None)


@pytest.mark.parametrize("recovery", ["idle", "failed", "rebound"])
async def test_failed_stop_settlement_preserves_intent_for_safe_reconciliation(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    recovery: str,
) -> None:
    store, ids = family
    child_id = ids["active"]
    old_row = store.get_conversation(child_id)
    settle = store.settle_intentionally_stopped_session
    sessions._intentional_stop_sessions[child_id] = _RUNNER
    attempts = 0

    def unavailable(*_args):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("metadata write temporarily unavailable")

    monkeypatch.setattr(store, "settle_intentionally_stopped_session", unavailable)
    error = ErrorDetail(code="runner_disconnected", message="Runner disappeared.")
    await sessions._mark_runner_sessions_offline([old_row], error, store)
    assert attempts == 3
    assert sessions._intentional_stop_sessions.get(child_id) == _RUNNER
    assert sessions._session_status_cache[child_id] == "running"
    assert store.get_conversation(child_id).live_status == "running"

    monkeypatch.setattr(store, "settle_intentionally_stopped_session", settle)
    if recovery == "failed":
        store.set_session_live_status(child_id, "failed")
        sessions._session_status_cache[child_id] = "failed"
    elif recovery == "rebound":
        store.replace_runner_id(child_id, "runner-replacement")
    if recovery != "idle":
        store.set_labels(child_id, {"omnigent.last_task_error_code": "preserved"})

    await sessions._mark_runner_sessions_offline([old_row], error, store)
    after = store.get_conversation(child_id)
    assert after.live_status == ("running" if recovery == "rebound" else recovery)
    assert sessions._session_status_cache[child_id] == after.live_status
    assert child_id not in sessions._intentional_stop_sessions
    if recovery != "idle":
        assert after.labels["omnigent.last_task_error_code"] == "preserved"
    if recovery == "rebound":
        assert after.runner_id == "runner-replacement"


@pytest.mark.parametrize("first_cancelled", [False, True])
@pytest.mark.parametrize("same_runner", [False, True])
async def test_overlapping_stop_failure_preserves_successful_stop(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    first_cancelled: bool,
    same_runner: bool,
) -> None:
    store, ids = family
    first_entered, release_first = asyncio.Event(), asyncio.Event()
    calls = 0

    async def teardown(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_entered.set()
            await release_first.wait()
            return False
        return True

    monkeypatch.setattr(sessions, "_stop_session_host_runner", teardown)
    first = asyncio.create_task(
        orchestration._stop_host_runner_intentionally(ids["parent"], "host", _RUNNER, None, store)
    )
    await asyncio.wait_for(first_entered.wait(), timeout=10)
    second_id = ids["parent"] if same_runner else ids["elsewhere"]
    second_runner = _RUNNER if same_runner else "runner-other"
    second = asyncio.create_task(
        orchestration._stop_host_runner_intentionally(
            second_id, "host", second_runner, None, store
        )
    )
    try:
        # Let an independent stop finish while the first delivery is held open.
        await asyncio.wait({second}, timeout=1)
        if not same_runner:
            assert second.done(), "Stopping another runner must not wait for the first runner"
        if first_cancelled:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            release_first.set()
            assert not await first
        assert await asyncio.wait_for(second, timeout=10)
        error = ErrorDetail(
            code="runner_disconnected", message="Runner disconnected unexpectedly."
        )
        await sessions._mark_runner_sessions_offline(
            store.list_conversations_by_runner_id(second_runner), error, store
        )
        child_id = ids["active"] if same_runner else ids["elsewhere"]
        assert sessions._session_status_cache[child_id] == "idle"
        assert (
            sessions._last_task_error_from_labels(store.get_conversation(child_id).labels) is None
        )
    finally:
        release_first.set()
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
