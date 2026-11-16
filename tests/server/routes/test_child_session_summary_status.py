"""Child-session summaries fall back to the persisted status on a cache miss.

The web Agents tree refetches ``GET /v1/sessions/{id}/child_sessions``. On a
replica that doesn't hold the runner's tunnel, the in-memory status cache has
no entry, so the summary must read the row's persisted ``live_status`` or a
refetch would flip a working row back to idle.
"""

from __future__ import annotations

import pytest

from omnigent.entities import Conversation
from omnigent.server.routes import sessions as sessions_routes
from omnigent.server.routes._sessions.helpers import (
    _child_session_summary_from_conversation,
)


def _child(live_status: str | None) -> Conversation:
    return Conversation(
        id="conv_child_status",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_parent",
        title="tool:child-task",
        agent_id="ag_test",
        live_status=live_status,
    )


@pytest.mark.parametrize(
    ("live_status", "busy", "task_status"),
    [
        ("running", True, "in_progress"),
        ("waiting", True, "in_progress"),
        ("idle", False, "completed"),
        ("failed", False, "failed"),
        (None, False, None),
    ],
)
def test_summary_falls_back_to_persisted_status_on_cache_miss(
    live_status: str | None, busy: bool, task_status: str | None
) -> None:
    conv = _child(live_status)
    sessions_routes._session_status_cache.pop(conv.id, None)

    summary = _child_session_summary_from_conversation(conv, "conv_parent", None)

    assert summary.busy is busy
    assert summary.current_task_status == task_status


def test_summary_prefers_the_live_cache_over_the_row() -> None:
    conv = _child("running")
    sessions_routes._session_status_cache[conv.id] = "idle"
    try:
        summary = _child_session_summary_from_conversation(conv, "conv_parent", None)
    finally:
        sessions_routes._session_status_cache.pop(conv.id, None)

    assert summary.busy is False
    assert summary.current_task_status == "completed"
