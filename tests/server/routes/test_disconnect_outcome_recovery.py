"""Disconnect-failure outcome logging on the real reconnect recovery path."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.debug_log_helpers import capture_debug_rows
from tests.server.routes.test_sessions_runner_relay import _ScriptedRunnerClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("passive", "next_step", "expected"),
    [
        (True, "resume", ["turn_resumed"]),
        (True, "user_turn", ["superseded_by_user_turn"]),
        (True, "user_turn_during_recovery", []),
        (True, "running_during_recovery", []),
        (True, "failure_during_recovery", []),
        (True, "failure_without_prior_token", []),
        (True, "unidentified_failure_without_prior_token", []),
        (True, "different_turn", []),
        (True, "different_runner", []),
        (True, "no_response_context", []),
        (True, "cached_response_context", ["turn_resumed"]),
        # An explicit rebind is already a user action; the old turn can't resume.
        (False, "resume", []),
    ],
)
async def test_disconnect_failure_outcome_after_reconnect(
    db_uri: str,
    passive: bool,
    next_step: str,
    expected: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect-cleared disconnect failure records whether its turn resumed."""
    from omnigent.runtime import disconnect_outcome, session_stream
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions.orchestration import _relay_runner_stream_once
    from omnigent.server.schemas import ErrorDetail, OutputTextDeltaEvent

    disconnect_outcome.reset_for_tests()
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(runner_id="runner-outcome")
    session_id = conv.id
    store.set_session_live_status(session_id, "running")
    sessions_module._session_status_cache[session_id] = "running"
    sessions_module._session_active_response_cache[session_id] = "turn-outcome"
    snapshot = store.get_conversation(session_id)
    assert snapshot is not None
    try:
        await sessions_module._mark_runner_sessions_offline(
            [snapshot],
            ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly."),
            store,
        )
        assert sessions_module._session_status_cache.get(session_id) == "failed"
        lookup = store.get_conversation
        if next_step in {
            "failure_without_prior_token",
            "unidentified_failure_without_prior_token",
        }:
            disconnect_outcome.note_user_turn(session_id)
        if next_step in {
            "user_turn_during_recovery",
            "running_during_recovery",
            "failure_during_recovery",
            "failure_without_prior_token",
            "unidentified_failure_without_prior_token",
        }:

            def change_during_lookup(conversation_id: str):
                row = lookup(conversation_id)
                if next_step == "user_turn_during_recovery":
                    disconnect_outcome.note_user_turn(conversation_id)
                elif next_step == "running_during_recovery":
                    sessions_module._publish_status(
                        conversation_id, "running", response_id="new-turn"
                    )
                else:
                    disconnect_outcome.note_disconnect_failure(
                        conversation_id,
                        runner_id="runner-outcome",
                        response_id=None
                        if next_step == "unidentified_failure_without_prior_token"
                        else "new-turn",
                    )
                    store.set_labels(
                        conversation_id,
                        {
                            "omnigent.last_task_error_code": "runner_disconnected",
                            "omnigent.last_task_error_message": "newer disconnect",
                        },
                    )
                return row

            monkeypatch.setattr(store, "get_conversation", change_during_lookup)
        with capture_debug_rows("server") as rows:
            await sessions_module._publish_runner_recovered_status(
                session_id, store, require_disconnect_code=passive
            )
            if next_step == "user_turn":
                disconnect_outcome.note_user_turn(session_id)
            event = OutputTextDeltaEvent(
                type="response.output_text.delta", delta="still working"
            ).model_dump(exclude_none=True)
            assert "response_id" not in event
            session_stream.publish(session_id, event)
            if next_step != "user_turn":
                assert not any(row["event_name"] == "session_turn_failure_outcome" for row in rows)
            release = asyncio.Event()
            release.set()
            response_id = "different-turn" if next_step == "different_turn" else "turn-outcome"
            lifecycle = [{"type": "response.in_progress", "response": {"id": response_id}}]
            if next_step in {"no_response_context", "cached_response_context"}:
                lifecycle = []
            if next_step == "cached_response_context":
                sessions_module._session_active_response_cache[session_id] = response_id
            client = _ScriptedRunnerClient(release, [*lifecycle, event])
            await _relay_runner_stream_once(
                session_id,
                client,
                store,  # type: ignore[arg-type]
                runner_id="different-runner"
                if next_step == "different_runner"
                else "runner-outcome",
            )
        expected_status = (
            "failed"
            if next_step
            in {
                "failure_during_recovery",
                "failure_without_prior_token",
                "unidentified_failure_without_prior_token",
            }
            else "running"
            if next_step == "running_during_recovery"
            else "idle"
        )
        assert sessions_module._session_status_cache.get(session_id) == expected_status
        persisted_error = sessions_module._last_task_error_from_labels(lookup(session_id).labels)
        if expected_status == "failed":
            assert persisted_error is not None
            assert persisted_error["message"] == "newer disconnect"
        elif expected_status == "idle":
            assert persisted_error is None
        outcomes = [
            row["attributes"]["outcome"]
            for row in rows
            if row["event_name"] == "session_turn_failure_outcome"
        ]
        assert outcomes == expected
    finally:
        disconnect_outcome.reset_for_tests()
        sessions_module._session_status_cache.pop(session_id, None)
        sessions_module._session_active_response_cache.pop(session_id, None)
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
