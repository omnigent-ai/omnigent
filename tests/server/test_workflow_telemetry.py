"""Workflow facts survive incomplete ancestry and optional logging lookups."""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnigent.server.workflow_telemetry import log_journal_entry
from omnigent.stores.conversation_store import (
    CREATED_BY_SESSION_LABEL_KEY,
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_journal_store import SessionJournalStore
from tests.debug_log_helpers import capture_debug_rows


@pytest.mark.parametrize(
    ("ancestry", "expected"),
    [
        ("missing", "unknown"),
        ("cycle", "unknown"),
        ("cycle_with_root", "unknown"),
        ("shared_ancestor", "interactive"),
        ("automation_and_missing", "automation"),
        ("lookup_failure", "unknown"),
    ],
)
def test_incomplete_ancestry_is_not_reported_as_interactive(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, ancestry: str, expected: str
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation()
    child = conversations.create_conversation(parent_conversation_id=root.id)
    session = conversations.create_conversation(parent_conversation_id=child.id)
    creator = root.id if ancestry in {"shared_ancestor", "cycle"} else uuid4().hex
    if ancestry in {"cycle", "cycle_with_root"}:
        conversations.set_labels(root.id, {CREATED_BY_SESSION_LABEL_KEY: session.id})
    if ancestry == "cycle_with_root":
        creator = conversations.create_conversation().id
    if ancestry == "automation_and_missing":
        conversations.set_labels(root.id, {SCHEDULED_TASK_ORIGIN_LABEL_KEY: uuid4().hex})
    conversations.set_labels(session.id, {CREATED_BY_SESSION_LABEL_KEY: creator})
    session = conversations.get_conversation(session.id)
    assert session is not None
    plan = (
        SessionJournalStore(db_uri)
        .append(
            session.id,
            "plan",
            {"name": "Private plan", "steps": [{"id": "private-step", "name": "Private task"}]},
            idempotency_key="plan",
        )
        .entry
    )
    if ancestry == "lookup_failure":

        def unavailable(session_id: str):
            raise RuntimeError("Metadata database unavailable")

        # The committed journal fact remains observable during a metadata-read outage.
        monkeypatch.setattr(conversations, "get_conversation", unavailable)
    with capture_debug_rows("server") as rows:
        log_journal_entry(conversations, session, plan, None, user_id="alice@example.com")
    [row] = [row for row in rows if row["event_name"] == "workflow_plan_written"]
    assert row["attributes"]["execution_source"] == expected
    assert row["attributes"]["plan_id"] == plan.id
    if expected == "automation":
        assert row["attributes"]["automation_session_id"] == root.id
    assert "Private" not in str(row) and "private-step" not in str(row)
