"""Child-session summaries agree with the updates stream on a sub-agent's status.

The web Agents tree patches rows from ``WS /v1/sessions/updates`` frames and
refetches ``GET /v1/sessions/{id}/child_sessions`` only occasionally. On a
replica that doesn't hold the runner's tunnel, the in-memory status cache
misses; both paths must then read the row's persisted ``live_status``, or a
refetch would flip a working row back to idle.
"""

from __future__ import annotations

import pytest

from omnigent.entities import Conversation
from omnigent.server.routes import sessions as sessions_routes
from omnigent.server.routes._sessions.helpers import (
    _child_session_summary_from_conversation,
    _session_own_status,
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
    ("live_status", "busy", "task_status", "own_status"),
    [
        ("running", True, "in_progress", "running"),
        ("waiting", True, "in_progress", "running"),
        ("idle", False, "completed", "idle"),
        ("failed", False, "failed", "failed"),
        (None, False, None, None),
    ],
)
def test_summary_falls_back_to_persisted_status_on_cache_miss(
    live_status: str | None, busy: bool, task_status: str | None, own_status: str | None
) -> None:
    conv = _child(live_status)
    sessions_routes._session_status_cache.pop(conv.id, None)

    summary = _child_session_summary_from_conversation(conv, "conv_parent", None)

    assert summary.busy is busy
    assert summary.current_task_status == task_status
    # The stream's row reports the same status for the same session.
    assert _session_own_status(conv.id, conv.live_status) == own_status


def test_summary_prefers_the_live_cache_over_the_row() -> None:
    conv = _child("running")
    sessions_routes._session_status_cache[conv.id] = "idle"
    try:
        summary = _child_session_summary_from_conversation(conv, "conv_parent", None)
    finally:
        sessions_routes._session_status_cache.pop(conv.id, None)

    assert summary.busy is False
    assert summary.current_task_status == "completed"
