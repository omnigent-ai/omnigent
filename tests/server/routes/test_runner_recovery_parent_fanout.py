"""Regression: clearing a child's stale runner-disconnect failure on reconnect
must notify the parent's rail, not only the child's own stream."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import Mock

import pytest

from omnigent.runtime import session_stream
from omnigent.server import session_live_state
from omnigent.server.routes._sessions import common, helpers, orchestration
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_CODE_KEY = common._LAST_TASK_ERROR_CODE_LABEL_KEY
_MESSAGE_KEY = common._LAST_TASK_ERROR_MESSAGE_LABEL_KEY


async def _flush_live_state() -> None:
    done = threading.Event()
    session_live_state.submit("test_barrier", done.set)
    assert await asyncio.to_thread(done.wait, 10)


def _seed_failed(store: SqlAlchemyConversationStore, session_id: str, code: str) -> None:
    """Leave ``session_id`` in the state the relay persists for a failed turn."""
    store.set_session_live_status(session_id, "failed")
    common._session_status_cache[session_id] = "failed"
    store.set_labels(session_id, {_CODE_KEY: code, _MESSAGE_KEY: f"{code} mid-turn"})


@dataclass
class _RecoveryOutcome:
    events: list[tuple[str, dict[str, Any]]]
    cached_status: str | None

    def events_for(self, session_id: str) -> list[dict[str, Any]]:
        return [event for target, event in self.events if target == session_id]


async def _recover_passively(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
    *,
    cleanup: tuple[str, ...],
    during_label_clear: Callable[[], None] | None = None,
) -> _RecoveryOutcome:
    """Run the passive-reconnect recovery for ``session_id`` and capture every publish.

    ``during_label_clear`` runs while recovery is parked on its label write, so a
    test can land another status edge at exactly that point.
    """
    published = Mock()
    monkeypatch.setattr(session_stream, "publish", published)
    if during_label_clear is not None:
        original = orchestration._persist_session_status_error_labels

        async def interleaved(*args: Any, **kwargs: Any) -> None:
            during_label_clear()
            await original(*args, **kwargs)

        monkeypatch.setattr(orchestration, "_persist_session_status_error_labels", interleaved)
    session_live_state.configure(store)
    try:
        await orchestration._publish_runner_recovered_status_impl(
            session_id, store, require_disconnect_code=True
        )
        await _flush_live_state()
        return _RecoveryOutcome(
            events=[(call.args[0], call.args[1]) for call in published.call_args_list],
            cached_status=common._session_status_cache.get(session_id),
        )
    finally:
        await _flush_live_state()
        session_live_state.configure(None)
        for cid in cleanup:
            common._session_status_cache.pop(cid, None)


@pytest.mark.asyncio
async def test_runner_recovery_notifies_parent_of_cleared_child(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing the child's sticky ``runner_disconnected`` failure on a passive
    reconnect must reach the parent's Agents rail, not only the child's stream."""
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id)

    store.set_session_live_status(parent.id, "running")
    common._session_status_cache[parent.id] = "running"
    _seed_failed(store, child.id, "runner_disconnected")

    outcome = await _recover_passively(store, monkeypatch, child.id, cleanup=(parent.id, child.id))

    child_events = outcome.events_for(child.id)
    assert [event["type"] for event in child_events] == ["session.status"]
    assert child_events[0]["status"] == "idle"
    assert outcome.cached_status == "idle"

    parent_events = outcome.events_for(parent.id)
    assert len(parent_events) == 1, (
        "parent rail was not notified that the child recovered; recovery only "
        "published to the child's own stream, leaving a stale Failed child on the parent"
    )
    assert parent_events[0]["type"] == "session.child_session.updated"
    assert parent_events[0]["child_session_id"] == child.id
    assert parent_events[0]["child"]["busy"] is False
    # The summary is built after the disconnect labels clear; otherwise the rail
    # would be told "idle" while still carrying the failed cause.
    assert parent_events[0]["child"]["last_task_error"] is None
    assert parent_events[0]["child"]["current_task_status"] == "completed"


@pytest.mark.asyncio
async def test_runner_recovery_fan_out_follows_a_running_edge_landing_mid_clear(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``running`` edge published while recovery clears the labels must not be
    followed by a stale idle summary on the parent."""
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id)
    _seed_failed(store, child.id, "runner_disconnected")

    outcome = await _recover_passively(
        store,
        monkeypatch,
        child.id,
        cleanup=(parent.id, child.id),
        during_label_clear=lambda: helpers._publish_status(child.id, "running"),
    )

    assert outcome.cached_status == "running"
    summaries = [
        (
            event["child"]["busy"],
            event["child"]["current_task_status"],
            event["child"]["last_task_error"],
        )
        for event in outcome.events_for(parent.id)
    ]
    assert summaries, "the parent heard nothing about the child"
    assert summaries[-1] == (True, "in_progress", None), (
        f"the parent's last summary is stale after the running edge: {summaries}"
    )
