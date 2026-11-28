"""Disconnect-failure outcome logging on the real reconnect recovery path."""

from __future__ import annotations

import asyncio
import threading

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.debug_log_helpers import capture_debug_rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("passive", "next_step", "expected"),
    [
        (True, "resume", ["turn_resumed"]),
        (True, "user_turn", ["superseded_by_user_turn"]),
        # An explicit rebind is already a user action; the old turn can't resume.
        (False, "resume", []),
    ],
)
async def test_disconnect_failure_outcome_after_reconnect(
    db_uri: str, passive: bool, next_step: str, expected: list[str]
) -> None:
    """A reconnect-cleared disconnect failure records whether its turn resumed."""
    from omnigent.runtime import disconnect_outcome, session_stream
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.schemas import ErrorDetail

    disconnect_outcome.reset_for_tests()
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(runner_id="runner-outcome")
    session_id = conv.id
    store.set_session_live_status(session_id, "running")
    sessions_module._session_status_cache[session_id] = "running"
    snapshot = store.get_conversation(session_id)
    assert snapshot is not None
    try:
        await sessions_module._mark_runner_sessions_offline(
            [snapshot],
            ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly."),
            store,
        )
        assert sessions_module._session_status_cache.get(session_id) == "failed"
        with capture_debug_rows("server") as rows:
            await sessions_module._publish_runner_recovered_status(
                session_id, store, require_disconnect_code=passive
            )
            if next_step == "user_turn":
                disconnect_outcome.note_user_turn(session_id)
            session_stream.publish(
                session_id, {"type": "response.output_text.delta", "delta": "still working"}
            )
        assert sessions_module._session_status_cache.get(session_id) == "idle"
        outcomes = [
            row["attributes"]["outcome"]
            for row in rows
            if row["event_name"] == "session_turn_failure_outcome"
        ]
        assert outcomes == expected
    finally:
        disconnect_outcome.reset_for_tests()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_passive_reconnect_keeps_genuine_failure_unwatched(db_uri: str) -> None:
    """A real task failure survives a reconnect and is never marked resumed."""
    from omnigent.runtime import disconnect_outcome, session_stream
    from omnigent.server.routes import sessions as sessions_module

    disconnect_outcome.reset_for_tests()
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(runner_id="runner-genuine")
    store.set_labels(
        conv.id,
        {
            "omnigent.last_task_error_code": "native_turn_error",
            "omnigent.last_task_error_message": "Synthetic provider rejection",
        },
    )
    sessions_module._session_status_cache[conv.id] = "failed"
    try:
        with capture_debug_rows("server") as rows:
            await sessions_module._publish_runner_recovered_status(
                conv.id, store, require_disconnect_code=True
            )
            session_stream.publish(conv.id, {"type": "session.status", "status": "running"})
        assert sessions_module._session_status_cache.get(conv.id) == "failed"
        assert not any(
            row["event_name"] in ("session_turn_failure_cleared", "session_turn_failure_outcome")
            for row in rows
        )
    finally:
        disconnect_outcome.reset_for_tests()
        sessions_module._session_status_cache.pop(conv.id, None)
        session_stream.close(conv.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_state", ["running", "failed", "rebound", "handoff"])
async def test_passive_reconnect_preserves_changes_during_lookup(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, newer_state: str
) -> None:
    """A delayed disconnect lookup cannot settle a newer turn or runner."""
    from omnigent.runtime import disconnect_outcome, session_stream
    from omnigent.server import session_live_state
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions.common import _RelayHandle, _session_recovery_tokens
    from omnigent.server.schemas import ErrorDetail

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(runner_id="runner-old")
    await sessions_module._persist_session_status_error_labels(
        conv.id, ErrorDetail(code="runner_disconnected", message="Old disconnect"), store
    )
    sessions_module._session_status_cache[conv.id] = "failed"
    original_get = store.get_conversation
    lookup_started = threading.Event()
    finish_lookup = threading.Event()

    def delayed_lookup(session_id: str):
        snapshot = original_get(session_id)
        lookup_started.set()
        assert finish_lookup.wait(5)
        return snapshot

    monkeypatch.setattr(store, "get_conversation", delayed_lookup)
    recovery = asyncio.create_task(
        sessions_module._publish_runner_recovered_status(
            conv.id, store, require_disconnect_code=True
        )
    )
    newer_failure = None
    try:
        assert await asyncio.to_thread(lookup_started.wait, 5)
        with capture_debug_rows("server") as rows:
            if newer_state in ("running", "failed"):
                sessions_module._publish_status(conv.id, "running")
                if newer_state == "failed":
                    failure = ErrorDetail(code="native_turn_error", message="New provider failure")
                    sessions_module._publish_status(conv.id, "failed", failure)
                    newer_failure = asyncio.create_task(
                        sessions_module._persist_session_status_error_labels(
                            conv.id, failure, store
                        )
                    )
            elif newer_state == "rebound":
                task = asyncio.current_task()
                assert task is not None
                sessions_module._runner_relay_tasks[conv.id] = _RelayHandle(
                    runner_id="runner-new", task=task, ready=asyncio.Event()
                )
            else:
                session_live_state.forget_live_status(conv.id)
            finish_lookup.set()
            await asyncio.wait_for(recovery, 5)
            if newer_failure is not None:
                await asyncio.wait_for(newer_failure, 5)
        assert sessions_module._session_status_cache.get(conv.id) == (
            "running" if newer_state == "running" else "failed"
        )
        saved = original_get(conv.id)
        assert saved is not None
        assert sessions_module._last_task_error_from_labels(saved.labels) == {
            "code": "native_turn_error" if newer_state == "failed" else "runner_disconnected",
            "message": "New provider failure" if newer_state == "failed" else "Old disconnect",
        }
        assert not any(
            row["event_name"] in ("session_turn_failure_cleared", "session_turn_failure_outcome")
            for row in rows
        )
        assert _session_recovery_tokens.get(conv.id) is None
    finally:
        finish_lookup.set()
        await asyncio.wait_for(recovery, 5)
        if newer_failure is not None:
            await asyncio.wait_for(newer_failure, 5)
        sessions_module._runner_relay_tasks.pop(conv.id, None)
        sessions_module._session_status_cache.pop(conv.id, None)
        session_stream.close(conv.id)
        disconnect_outcome.reset_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked_code",
    [
        pytest.param("", id="clear-before-failure"),
        pytest.param("native_turn_error", id="failure-before-recovery"),
    ],
)
async def test_recovery_and_failure_label_writes_follow_status_order(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, blocked_code: str
) -> None:
    """Recovery cannot erase a newer failure before or after its snapshot read."""
    from omnigent.runtime import disconnect_outcome, session_stream
    from omnigent.server import session_live_state
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.schemas import ErrorDetail

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(runner_id="runner-recovery")
    await sessions_module._persist_session_status_error_labels(
        conv.id, ErrorDetail(code="runner_disconnected", message="Old disconnect"), store
    )
    sessions_module._session_status_cache[conv.id] = "failed"
    original_set = store.set_labels
    write_started = threading.Event()
    finish_write = threading.Event()
    later_operation_queued = asyncio.Event()
    original_submit = session_live_state.submit

    def delayed_write(session_id: str, labels: dict[str, str]) -> None:
        if labels.get("omnigent.last_task_error_code") == blocked_code:
            write_started.set()
            assert finish_write.wait(5)
        original_set(session_id, labels)

    def record_submit(description, function, *args, **kwargs):
        future = original_submit(description, function, *args, **kwargs)
        if description in ("session_status_error_labels", "runner_recovery_snapshot"):
            later_operation_queued.set()
        return future

    failure = ErrorDetail(code="native_turn_error", message="New provider failure")

    async def fail_after_persistence() -> None:
        await sessions_module._persist_session_status_error_labels(conv.id, failure, store)
        sessions_module._publish_status(conv.id, "failed", failure)

    monkeypatch.setattr(store, "set_labels", delayed_write)
    monkeypatch.setattr(session_live_state, "submit", record_submit)
    first = asyncio.create_task(
        sessions_module._publish_runner_recovered_status(conv.id, store)
        if blocked_code == ""
        else fail_after_persistence()
    )
    second = None
    try:
        assert await asyncio.to_thread(write_started.wait, 5)
        later_operation_queued.clear()
        with capture_debug_rows("server") as rows:
            if blocked_code == "":
                sessions_module._publish_status(conv.id, "failed", failure)
                second = asyncio.create_task(
                    sessions_module._persist_session_status_error_labels(conv.id, failure, store)
                )
            else:
                second = asyncio.create_task(
                    sessions_module._publish_runner_recovered_status(
                        conv.id, store, require_disconnect_code=True
                    )
                )
            await asyncio.wait_for(later_operation_queued.wait(), 5)
            finish_write.set()
            await asyncio.wait_for(asyncio.gather(first, second), 5)
        saved = store.get_conversation(conv.id)
        assert saved is not None
        assert sessions_module._last_task_error_from_labels(saved.labels) == {
            "code": "native_turn_error",
            "message": "New provider failure",
        }
        assert sessions_module._session_status_cache.get(conv.id) == "failed"
        assert not any(row["event_name"] == "session_turn_failure_cleared" for row in rows)
    finally:
        finish_write.set()
        await asyncio.wait_for(first, 5)
        if second is not None:
            await asyncio.wait_for(second, 5)
        sessions_module._session_status_cache.pop(conv.id, None)
        session_stream.close(conv.id)
        disconnect_outcome.reset_for_tests()
