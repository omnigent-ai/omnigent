"""Session projection and sparse-filter scan limits without a server fixture."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnigent.db.utils import now_epoch
from omnigent.entities import Conversation
from omnigent.entities.work_view import ViewFilters, ViewQuery
from omnigent.server.routes.work_views import query_session_view, session_view_status
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.project_store import ProjectStore


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"archived": True, "live_status": "running"}, "archived"),
        ({"pending_elicitation_count": 1, "live_status": "running"}, "needs_input"),
        ({"live_status": "failed"}, "failed"),
        ({"runner_last_seen": None, "live_status": "running"}, "offline"),
        ({"background_work_running": True, "live_status": "idle"}, "running"),
        ({"live_status": "waiting"}, "waiting"),
        ({"live_status": None}, "idle"),
    ],
)
def test_session_status_projection(state: dict, expected: str) -> None:
    conversation = Conversation(
        id="a" * 32,
        root_conversation_id="a" * 32,
        created_at=1,
        updated_at=1,
        runner_last_seen=now_epoch(),
    )
    assert session_view_status(replace(conversation, **state)) == expected


def test_sparse_session_query_returns_continuation_after_bounded_scan() -> None:
    conversations, projects = Mock(spec=ConversationStore), Mock(spec=ProjectStore)
    projects.list.return_value = []
    rows = [
        Conversation(
            id=f"{index:032x}",
            root_conversation_id=f"{index:032x}",
            created_at=index,
            updated_at=index,
        )
        for index in range(1001)
    ]

    def page(**kwargs):
        start = int(kwargs["after"], 16) + 1 if kwargs["after"] else 0
        end = min(len(rows), start + kwargs["limit"])
        return SimpleNamespace(data=rows[start:end], has_more=end < len(rows))

    conversations.list_conversations.side_effect = page
    query = ViewQuery(entity_type="sessions", filters=ViewFilters(statuses=["running"]), limit=1)
    first = query_session_view(query, "alice", conversations, projects)
    assert first == {"data": [], "has_more": True, "next_cursor": rows[999].id}
    assert conversations.list_conversations.call_count == 5
    query.cursor = first["next_cursor"]
    assert query_session_view(query, "alice", conversations, projects) == {
        "data": [],
        "has_more": False,
        "next_cursor": None,
    }
