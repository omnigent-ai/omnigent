"""Shutdown attribution must survive replica handoff without hiding another turn."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from omnigent.entities import Conversation
from omnigent.host.frames import HostHelloFrame
from omnigent.host.shutdown import ShutdownIntent, timestamp_ms
from omnigent.server import session_live_state
from omnigent.server import shutdown_attribution as attribution
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._sessions.common import _session_status_cache
from omnigent.server.routes._sessions.helpers import (
    _last_task_error_from_labels,
    _persist_session_status_error_labels,
    _publish_status,
)
from omnigent.server.routes._sessions.orchestration import _mark_runner_sessions_offline_impl
from omnigent.server.schemas import ErrorDetail
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

pytestmark = pytest.mark.asyncio
_HOST = "a" * 32
_OTHER_HOST = "b" * 32
_RUNNER = "runner_shutdown_test"
_CONNECTION = "connection_one"


@pytest.fixture
async def store(db_uri: str) -> AsyncIterator[SqlAlchemyConversationStore]:
    value = SqlAlchemyConversationStore(db_uri)
    attribution.session_scopes.clear()
    attribution.session_shutdowns.clear()
    attribution.runner_connections.clear()
    session_live_state.configure(value)
    yield value
    await session_live_state.drain_pending_writes()
    session_live_state.configure(None)
    attribution.session_scopes.clear()
    attribution.session_shutdowns.clear()
    attribution.runner_connections.clear()
    _session_status_cache.clear()


async def _running(store: SqlAlchemyConversationStore, **kwargs: object) -> Conversation:
    conv = store.create_conversation(runner_id=_RUNNER, **kwargs)
    await attribution.begin_connection(conv.id, _RUNNER, _CONNECTION, store)
    _publish_status(conv.id, "running")
    await session_live_state.drain_pending_writes()
    return store.get_conversation(conv.id)


def _intent(**kwargs: object) -> ShutdownIntent:
    return ShutdownIntent.model_validate(
        {
            "reason": "user_stopped_host",
            "action": "host_stop",
            "initiator": "local_cli",
            "host_id": _HOST,
            "host_process_id": "process_one",
            "host_connection_id": "host_conn",
            **kwargs,
        }
    )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"force": True},
        {"daemon_only": True},
        {"action": "host_disable"},
        {
            "reason": "user_stopped_session",
            "action": "stop_session",
            "initiator": "authenticated_user",
        },
        {
            "reason": "host_interrupted_sigint",
            "action": "signal",
            "signal_name": "SIGINT",
            "initiator": "unknown",
        },
    ],
)
async def test_requested_shutdown_settles_only_bound_lifecycle(
    store: SqlAlchemyConversationStore,
    fields: dict[str, object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    conv = await _running(store, host_id=_HOST, workspace="/tmp")
    intent = _intent(**fields)
    evidence = await attribution.record_session_shutdown(conv, intent, store)
    assert evidence is not None
    # The runner-disconnect replica has no host-tunnel or session caches.
    attribution.session_scopes.clear()
    attribution.session_shutdowns.clear()
    _session_status_cache.clear()
    await _mark_runner_sessions_offline_impl(
        [conv],
        ErrorDetail(code="runner_disconnected", message="transport lost"),
        store,
        connection_id=_CONNECTION,
        lost_at_ms=timestamp_ms(),
    )
    saved = store.get_conversation(conv.id)
    assert saved.live_status == "idle"
    assert _last_task_error_from_labels(saved.labels) is None
    events = {getattr(record, "event_name", None) for record in caplog.records}
    assert {"session_shutdown_requested", "session_shutdown_applied"} <= events
    assert "session_turn_failed" not in events
    if intent.signal_name == "SIGINT":
        assert intent.category == "Host stopped by interrupt (SIGINT)"
        assert evidence.intent.initiator == "unknown"
        assert evidence.intent.initiator_user_id is None


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGHUP", None])
async def test_unknown_signal_or_unobserved_death_remains_failure(
    store: SqlAlchemyConversationStore,
    signal_name: str | None,
) -> None:
    conv = await _running(store)
    if signal_name is not None:
        evidence = await attribution.record_session_shutdown(
            conv,
            _intent(
                reason="unknown", action="signal", initiator="unknown", signal_name=signal_name
            ),
            store,
        )
        assert evidence is None
    await _mark_runner_sessions_offline_impl(
        [conv],
        ErrorDetail(code="runner_disconnected", message="transport lost"),
        store,
        connection_id=_CONNECTION,
        lost_at_ms=timestamp_ms(),
    )
    await session_live_state.drain_pending_writes()
    assert store.get_conversation(conv.id).live_status == "failed"


async def test_stop_preserves_prior_error_and_real_crash_report(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = await _running(store)
    error = ErrorDetail(code="runner_failed_to_start", message="earlier crash")
    await _persist_session_status_error_labels(conv.id, error, store)
    _publish_status(conv.id, "failed", error)
    await session_live_state.drain_pending_writes()
    failed = store.get_conversation(conv.id)
    evidence = await attribution.record_session_shutdown(failed, _intent(), store)
    assert evidence is not None
    assert await attribution.settle_shutdown(conv.id, evidence, store)
    assert store.get_conversation(conv.id).live_status == "failed"
    assert store.get_conversation(conv.id).labels == failed.labels

    # A crash report is direct evidence, even if a command is pending.
    other = await _running(store)
    await attribution.record_session_shutdown(other, _intent(), store)
    await _mark_runner_sessions_offline_impl([other], error, store, fail_idle_top_level=True)
    await session_live_state.drain_pending_writes()
    assert store.get_conversation(other.id).live_status == "failed"
    assert (
        _last_task_error_from_labels(store.get_conversation(other.id).labels)["message"]
        == "earlier crash"
    )


async def test_matching_requires_connection_order_and_ttl(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = await _running(store)
    evidence = await attribution.record_session_shutdown(conv, _intent(), store)
    assert evidence is not None
    for options in (
        {"runner_id": "other"},
        {"connection_id": "replacement"},
        {"lost_at_ms": evidence.recorded_at_ms - 1},
        {"lost_at_ms": evidence.recorded_at_ms + 120_001},
    ):
        assert await attribution.matching_shutdown(conv.id, store, **options) is None
    assert (
        await attribution.matching_shutdown(conv.id, store, connection_id=_CONNECTION) == evidence
    )
    await attribution.begin_connection(conv.id, _RUNNER, "replacement", store)
    assert await attribution.matching_shutdown(conv.id, store, connection_id="replacement") is None


async def test_cold_replica_new_turn_invalidates_shared_intent_and_old_settlement(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = await _running(store)
    evidence = await attribution.record_session_shutdown(conv, _intent(), store)
    assert evidence is not None
    matched = await attribution.matching_shutdown(conv.id, store)
    assert matched == evidence
    attribution.session_scopes.clear()
    attribution.session_shutdowns.clear()
    newer = await attribution.advance_lifecycle(conv.id, store, "next_turn")
    assert newer is not None and newer != evidence.scope
    _publish_status(conv.id, "running")
    await session_live_state.drain_pending_writes()
    # The old replica retains the scope it matched before the other replica advanced.
    attribution.session_scopes[conv.id] = evidence.scope
    assert not await attribution.settle_shutdown(conv.id, evidence, store)
    assert store.get_conversation(conv.id).live_status == "running"
    assert store.get_shutdown_state(conv.id)["intent"] is None


async def test_settlement_cas_cannot_overwrite_intervening_new_turn(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = await _running(store)
    evidence = await attribution.record_session_shutdown(conv, _intent(), store)
    assert evidence is not None
    original = store.compare_shutdown_state
    new_scope = evidence.scope.model_copy(update={"lifecycle_id": "new_turn"}).model_dump_json()

    def racing_settle(session_id: str, **kwargs: object) -> bool:
        if kwargs.get("settle"):
            assert original(
                session_id,
                runner_id=_RUNNER,
                expected_scope=evidence.scope.model_dump_json(),
                scope=new_scope,
                intent=None,
            )
            store.set_session_live_status(session_id, "running", expected_shutdown_scope=new_scope)
        return original(session_id, **kwargs)

    monkeypatch.setattr(store, "compare_shutdown_state", racing_settle)
    assert not await attribution.settle_shutdown(conv.id, evidence, store)
    assert store.get_conversation(conv.id).live_status == "running"


async def test_settlement_and_scope_change_do_not_poison_status_dedupe(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = await _running(store)
    evidence = await attribution.record_session_shutdown(conv, _intent(), store)
    assert evidence is not None
    assert await attribution.settle_shutdown(conv.id, evidence, store)
    assert store.get_conversation(conv.id).live_status == "idle"
    await attribution.advance_lifecycle(conv.id, store)
    _publish_status(conv.id, "running")
    await session_live_state.drain_pending_writes()
    assert store.get_conversation(conv.id).live_status == "running"
    # The cold disconnect replica must see the new turn and fail its real loss.
    _session_status_cache.clear()
    attribution.session_scopes.clear()
    await _mark_runner_sessions_offline_impl(
        [store.get_conversation(conv.id)],
        ErrorDetail(code="runner_disconnected", message="next run died"),
        store,
    )
    await session_live_state.drain_pending_writes()
    assert store.get_conversation(conv.id).live_status == "failed"


async def test_rejected_conditional_status_write_evicts_dedupe(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = await _running(store)
    old = attribution.session_scopes[conv.id]
    newer = await attribution.advance_lifecycle(conv.id, store)
    store.set_session_live_status(conv.id, "idle")
    attribution.session_scopes[conv.id] = old
    session_live_state.persist_live_status(conv.id, "running")
    await session_live_state.drain_pending_writes()
    assert conv.id not in session_live_state._last_status
    assert store.get_conversation(conv.id).live_status == "idle"
    attribution.session_scopes[conv.id] = newer
    session_live_state.persist_live_status(conv.id, "running")
    await session_live_state.drain_pending_writes()
    assert store.get_conversation(conv.id).live_status == "running"


@pytest.mark.parametrize("status", ["running", "failed"])
async def test_conditional_live_status_rejects_rebound_runner_before_new_scope(
    store: SqlAlchemyConversationStore, status: str
) -> None:
    conv = await _running(store)
    old = attribution.session_scopes[conv.id]
    store.replace_runner_id(conv.id, "replacement-runner")
    store.set_session_live_status(conv.id, "idle")
    session_live_state.forget_live_status(conv.id)

    # Binding changes before the replacement's connect callback initializes its scope.
    session_live_state.persist_live_status(conv.id, status)
    await session_live_state.drain_pending_writes()
    assert conv.id not in session_live_state._last_status
    assert store.get_shutdown_state(conv.id)["scope"] == old.model_dump_json()
    assert store.get_conversation(conv.id).live_status == "idle"

    await attribution.begin_connection(conv.id, "replacement-runner", "new-connection", store)
    session_live_state.persist_live_status(conv.id, "running")
    await session_live_state.drain_pending_writes()
    assert store.get_conversation(conv.id).live_status == "running"


async def test_duplicate_notification_keeps_settlement_and_metadata_private(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = await _running(store)
    intent = _intent()
    evidence = await attribution.record_session_shutdown(conv, intent, store)
    assert evidence is not None
    assert await attribution.settle_shutdown(conv.id, evidence, store)
    assert await attribution.record_session_shutdown(conv, intent, store) == evidence
    assert store.get_shutdown_state(conv.id)["settled"] is True
    store.set_session_state(conv.id, {"policy": "state"})
    assert store.get_conversation(conv.id).session_state == {"policy": "state"}
    assert store.get_shutdown_state(conv.id)["intent"] == evidence.model_dump_json()
    assert not store.set_session_live_status(
        conv.id, "running", expected_shutdown_scope=evidence.scope.model_dump_json()
    )
    fork = store.fork_conversation(conv.id)
    assert not store.get_shutdown_state(fork.id).get("intent")


async def test_host_evidence_checks_incarnation_ownership_actor_and_clock_skew(
    store: SqlAlchemyConversationStore,
) -> None:
    root = await _running(store, host_id=_HOST, workspace="/tmp")
    child = await _running(store, parent_conversation_id=root.id, kind="sub_agent")
    foreign = await _running(store, host_id=_OTHER_HOST, workspace="/tmp")
    registry = HostRegistry()
    conn = registry.register(
        _HOST,
        AsyncMock(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="test",
            runners=[_RUNNER],
            process_id="process_one",
            connection_id="host_conn",
        ),
        owner="host-owner",
    )
    intent = _intent(initiator_user_id="spoofed", requested_at_ms=timestamp_ms() + 1000)
    assert await attribution.record_host_shutdown(conn, intent, [_RUNNER], registry, store)
    for conv in (root, child):
        evidence = await attribution.matching_shutdown(conv.id, store)
        assert evidence is not None
        assert evidence.intent.initiator == "local_cli"
        assert evidence.intent.initiator_user_id is None
        assert evidence.intent.requested_at_ms == intent.requested_at_ms
    assert await attribution.matching_shutdown(foreign.id, store) is None
    assert not await attribution.record_host_shutdown(
        conn, _intent(host_process_id="old"), [_RUNNER], registry, store
    )
    registry.register(_HOST, AsyncMock(), conn.hello, owner="host-owner")
    assert not await attribution.record_host_shutdown(conn, intent, [_RUNNER], registry, store)


async def test_failed_stop_cancellation_is_scoped(store: SqlAlchemyConversationStore) -> None:
    conv = await _running(store, host_id=_HOST, workspace="/tmp")
    _, evidence = await attribution.prepare_session_stop(conv, store, None, user_id="owner")
    assert len(evidence) == 1
    await attribution.cancel_shutdown(evidence, store)
    assert await attribution.matching_shutdown(conv.id, store) is None
    _, replacement = await attribution.prepare_session_stop(conv, store, None, user_id="owner")
    await attribution.cancel_shutdown(evidence, store)
    assert await attribution.matching_shutdown(conv.id, store) == replacement[0][1]


@pytest.mark.parametrize("path", ["offline", "relay"])
async def test_cancellation_between_match_and_settlement_does_not_hide_failure(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    from omnigent.server.routes._sessions import orchestration

    conv = await _running(store)
    evidence = await attribution.record_session_shutdown(conv, _intent(), store)
    assert evidence is not None
    settle = attribution.settle_shutdown

    async def cancel_then_settle(session_id, stop, target_store, **kwargs):
        await attribution.cancel_shutdown([(session_id, stop)], target_store)
        return await settle(session_id, stop, target_store, **kwargs)

    monkeypatch.setattr(attribution, "settle_shutdown", cancel_then_settle)
    if path == "offline":
        await _mark_runner_sessions_offline_impl(
            [conv],
            ErrorDetail(code="runner_disconnected", message="actual disconnect"),
            store,
            connection_id=_CONNECTION,
        )
    else:

        async def lost(*_args, **_kwargs):
            raise orchestration._RelayTransportLost(evidence=evidence, lost_at_ms=timestamp_ms())

        monkeypatch.setattr(orchestration, "_relay_runner_stream_once", lost)
        monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0)
        await orchestration._relay_runner_stream(conv.id, AsyncMock(), store)
    await session_live_state.drain_pending_writes()
    assert store.get_conversation(conv.id).live_status == "failed"
    assert (
        _last_task_error_from_labels(store.get_conversation(conv.id).labels)["code"]
        == "runner_disconnected"
    )


async def test_host_replica_drops_only_departed_runner_cache_before_handoff(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = await _running(store, host_id=_HOST, workspace="/tmp")
    old = attribution.session_scopes[conv.id]
    attribution.runner_connections[_RUNNER] = _CONNECTION
    new_scope = old.model_copy(update={"lifecycle_id": "new", "runner_connection_id": "replica_b"})
    assert store.compare_shutdown_state(
        conv.id,
        runner_id=_RUNNER,
        expected_scope=old.model_dump_json(),
        scope=new_scope.model_dump_json(),
        intent=None,
    )
    attribution.forget_connection(_RUNNER, _CONNECTION)
    assert _RUNNER not in attribution.runner_connections
    assert conv.id not in attribution.session_scopes
    evidence = await attribution.record_session_shutdown(conv, _intent(), store)
    assert evidence is not None and evidence.scope == new_scope
    assert await attribution.settle_shutdown(conv.id, evidence, store)
    # A delayed old disconnect must leave the new incarnation's cache intact.
    attribution.runner_connections[_RUNNER] = "replica_b"
    attribution.session_scopes[conv.id] = new_scope
    attribution.forget_connection(_RUNNER, _CONNECTION)
    assert attribution.runner_connections[_RUNNER] == "replica_b"
    assert attribution.session_scopes[conv.id] == new_scope


async def test_retired_connect_callback_cannot_replace_current_scope(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = await _running(store)
    old = attribution.session_scopes[conv.id]
    current = True
    original = store.get_shutdown_state

    def delayed_read(session_id: str):
        nonlocal current
        current = False
        return original(session_id)

    monkeypatch.setattr(store, "get_shutdown_state", delayed_read)
    assert (
        await attribution.begin_connection(
            conv.id, _RUNNER, "retired", store, is_current=lambda: current
        )
        is None
    )
    assert original(conv.id)["scope"] == old.model_dump_json()


@pytest.mark.parametrize("next_lifecycle", ["turn", "connection"])
async def test_delayed_duplicate_intent_cannot_arm_a_new_lifecycle(
    store: SqlAlchemyConversationStore,
    next_lifecycle: str,
) -> None:
    conv = await _running(store)
    original = _intent()
    assert await attribution.record_session_shutdown(conv, original, store) is not None
    if next_lifecycle == "turn":
        await attribution.advance_lifecycle(conv.id, store)
    else:
        await attribution.begin_connection(conv.id, _RUNNER, "new_connection", store)
    assert await attribution.record_session_shutdown(conv, original, store) is None
    assert await attribution.matching_shutdown(conv.id, store) is None
    # A new user action can still stop this lifecycle.
    newer = await attribution.record_session_shutdown(conv, _intent(), store)
    assert newer is not None and newer.intent.shutdown_id != original.shutdown_id


@pytest.mark.parametrize(
    "operation", ["get_conversation", "drain", "compare_shutdown_state", "get_shutdown_state"]
)
@pytest.mark.parametrize("path", ["offline", "relay"])
async def test_settlement_storage_failure_retries_or_reports_disconnect(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    path: str,
) -> None:
    from omnigent.server.routes._sessions import orchestration

    conv = await _running(store)
    evidence = await attribution.record_session_shutdown(conv, _intent(), store)
    assert evidence is not None
    original_settle = attribution.settle_shutdown
    attempts = 0

    async def settle_with_outage(session_id, stop, target_store, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            return await original_settle(session_id, stop, target_store, **kwargs)
        with monkeypatch.context() as patch:
            if operation == "drain":
                patch.setattr(
                    session_live_state,
                    "drain_pending_writes",
                    AsyncMock(side_effect=OSError("database unavailable")),
                )
            else:

                def unavailable(*_args, **_kwargs):
                    raise OSError("database unavailable")

                patch.setattr(target_store, operation, unavailable)
            applied = await original_settle(session_id, stop, target_store, **kwargs)
            assert applied is False
            return applied

    monkeypatch.setattr(attribution, "settle_shutdown", settle_with_outage)
    if path == "offline":
        await orchestration._mark_runner_sessions_offline_impl(
            [conv],
            ErrorDetail(code="runner_disconnected", message="actual loss"),
            store,
            connection_id=_CONNECTION,
        )
    else:

        async def lost(*_args, **_kwargs):
            raise orchestration._RelayTransportLost(evidence=evidence, lost_at_ms=timestamp_ms())

        monkeypatch.setattr(orchestration, "_relay_runner_stream_once", lost)
        monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0)
        await orchestration._relay_runner_stream(conv.id, AsyncMock(), store)
    await session_live_state.drain_pending_writes()
    saved = store.get_conversation(conv.id)
    assert attempts == (2 if path == "offline" else 1)
    assert saved.live_status == ("idle" if path == "offline" else "failed")
    if path == "offline":
        assert _last_task_error_from_labels(saved.labels) is None
    else:
        assert _last_task_error_from_labels(saved.labels)["code"] == "runner_disconnected"


async def test_old_replay_is_rejected_after_replay_ledger_expires(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = await _running(store)
    original = _intent()
    assert await attribution.record_session_shutdown(conv, original, store) is not None
    await attribution.advance_lifecycle(conv.id, store)
    monkeypatch.setattr(attribution, "timestamp_ms", lambda: original.requested_at_ms + 180_001)
    assert await attribution.record_session_shutdown(conv, original, store) is None
    assert await attribution.matching_shutdown(conv.id, store) is None


async def test_retired_connection_cannot_clear_current_local_stop(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = await _running(store)
    evidence = await attribution.record_session_shutdown(conv, _intent(), store)
    assert evidence is not None
    await attribution.begin_connection(
        conv.id, _RUNNER, "retired", store, is_current=lambda: False
    )
    assert attribution.session_shutdowns[conv.id] == evidence
    assert await attribution.matching_shutdown(conv.id, store) == evidence
