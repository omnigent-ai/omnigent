"""Conversation content search filters by the authenticated turn actor."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import text

from omnigent.db.db_models import SqlSessionPermission, workspace_scope
from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import MessageData
from omnigent.runner.tool_dispatch import dispatch_tool_locally
from omnigent.server.routes._sessions.orchestration import _forward_event_to_runner
from omnigent.server.schemas import SessionEventInput
from omnigent.spec.types import AgentSpec, LocalToolInfo
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.search_conversations import SearchConversationsTool


def _message(text: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="response_test",
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def test_search_filters_permissions_before_limit(tmp_path: Path) -> None:
    store = SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'search.db'}")
    other = store.create_conversation(workspace=str(tmp_path))
    own = store.create_conversation(workspace=str(tmp_path))
    store.append(other.id, [_message("sapphire sapphire sapphire confidential")])
    store.append(own.id, [_message("sapphire authorized")])
    with store._conv_session("test_search_acl") as session:
        session.add_all(
            [
                SqlSessionPermission(
                    user_id="other@example.com", conversation_id=other.id, level=4
                ),
                SqlSessionPermission(
                    user_id="caller@example.com", conversation_id=own.id, level=4
                ),
            ]
        )
        session.commit()

    matches = store.search("sapphire", limit=1, accessible_by="caller@example.com")
    assert len(matches) == 1
    assert matches[0].data == _message("sapphire authorized").data
    assert (
        store.search("sapphire", conversation_id=other.id, accessible_by="caller@example.com")
        == []
    )

    with patch("omnigent.runtime.get_conversation_store", return_value=store):
        allowed = SearchConversationsTool().invoke(
            json.dumps({"query": "sapphire", "limit": 1}),
            ToolContext(task_id="task", agent_id="agent", caller_user_id="caller@example.com"),
        )
        denied = SearchConversationsTool().invoke(
            json.dumps({"query": "sapphire"}),
            ToolContext(task_id="task", agent_id="agent"),
        )

    assert "authorized" in allowed
    assert "confidential" not in allowed
    assert "authenticated caller" in denied


def test_search_keeps_identical_conversation_ids_in_their_workspace(tmp_path: Path) -> None:
    store = SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'workspaces.db'}")
    with workspace_scope(1):
        own = store.create_conversation(workspace=str(tmp_path))
        store.append(own.id, [_message("sapphire authorized")])
        with store._conv_session("test_search_acl") as session:
            session.add(
                SqlSessionPermission(user_id="caller@example.com", conversation_id=own.id, level=4)
            )
            session.commit()
    with workspace_scope(2):
        other = store.create_conversation(workspace=str(tmp_path), conversation_id=own.id)
        store.append(other.id, [_message("sapphire confidential")])
        with store._conv_session("test_search_acl") as session:
            session.add(
                SqlSessionPermission(
                    user_id="other@example.com", conversation_id=other.id, level=4
                )
            )
            session.commit()

    with workspace_scope(1):
        matches = store.search("sapphire", limit=1, accessible_by="caller@example.com")

    assert len(matches) == 1
    assert matches[0].data == _message("sapphire authorized").data


def test_search_prefetches_permissions_from_separate_database(tmp_path: Path) -> None:
    store = SqlAlchemyConversationStore(
        f"sqlite:///{tmp_path / 'metadata.db'}",
        conversation_storage_location=f"sqlite:///{tmp_path / 'conversations.db'}",
    )
    denied = store.create_conversation(workspace=str(tmp_path))
    allowed = store.create_conversation(workspace=str(tmp_path))
    store.append(denied.id, [_message("sapphire confidential")])
    store.append(allowed.id, [_message("sapphire authorized")])
    with store._session("test_search_acl") as session:
        session.add(
            SqlSessionPermission(user_id="caller@example.com", conversation_id=allowed.id, level=4)
        )
        session.commit()

    matches = store.search("sapphire", limit=1, accessible_by="caller@example.com")
    assert [item.data for item in matches] == [_message("sapphire authorized").data]
    assert (
        store.search("sapphire", conversation_id=denied.id, accessible_by="caller@example.com")
        == []
    )


def test_search_matches_dashed_and_legacy_fts_ids(tmp_path: Path) -> None:
    store = SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'search-ids.db'}")
    conversation = store.create_conversation(workspace=str(tmp_path))
    dashed = store.append(conversation.id, [_message("sapphire dashed")])[0]
    legacy = store.append(conversation.id, [_message("emerald legacy")])[0]
    with store._conv_session("test_search_fts_ids") as session:
        session.add(
            SqlSessionPermission(
                user_id="caller@example.com", conversation_id=conversation.id, level=4
            )
        )
        session.execute(
            text(
                "UPDATE conversation_items_fts SET item_id = :new_item_id, "
                "conversation_id = :new_conversation_id "
                "WHERE item_id = :item_id AND conversation_id = :conversation_id"
            ),
            [
                {
                    "new_item_id": str(uuid.UUID(dashed.id)),
                    "new_conversation_id": str(uuid.UUID(conversation.id)),
                    "item_id": dashed.id,
                    "conversation_id": conversation.id,
                },
                {
                    "new_item_id": f"msg_{legacy.id}",
                    "new_conversation_id": f"conv_{conversation.id}",
                    "item_id": legacy.id,
                    "conversation_id": conversation.id,
                },
            ],
        )
        session.commit()

    assert [item.id for item in store.search("sapphire", accessible_by="caller@example.com")] == [
        dashed.id
    ]
    assert [item.id for item in store.search("emerald", accessible_by="caller@example.com")] == [
        legacy.id
    ]


def test_search_non_fts_branch_filters_before_limit(tmp_path: Path) -> None:
    store = SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'search-fallback.db'}")
    allowed = store.create_conversation(workspace=str(tmp_path))
    denied = store.create_conversation(workspace=str(tmp_path))
    store.append(allowed.id, [_message("sapphire authorized")])
    store.append(denied.id, [_message("sapphire confidential")])
    with store._conv_session("test_search_acl") as session:
        session.add(
            SqlSessionPermission(user_id="caller@example.com", conversation_id=allowed.id, level=4)
        )
        session.commit()

    with patch(
        "omnigent.stores.conversation_store.sqlalchemy_store._supports_fts5", return_value=False
    ):
        matches = store.search("sapphire", limit=1, accessible_by="caller@example.com")
    assert [item.data for item in matches] == [_message("sapphire authorized").data]


def test_runner_local_dispatch_preserves_authenticated_caller(tmp_path: Path) -> None:
    captured_contexts = []
    agent_spec = AgentSpec(
        spec_version=1,
        name="agent",
        local_tools=[
            LocalToolInfo(name="local_search", language="python", path="tools/search.py")
        ],
    )

    def capture_tool(_name: str, _args: str, context: ToolContext) -> str:
        captured_contexts.append(context)
        return "ok"

    async def dispatch() -> str:
        async with httpx.AsyncClient(
            base_url="http://runner.test",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
        ) as client:
            return await dispatch_tool_locally(
                tool_name="local_search",
                call_id="call-1",
                arguments="{}",
                response_id="response-1",
                harness_client=client,
                agent_spec=agent_spec,
                conversation_id="conversation-1",
                caller_user_id="caller@example.com",
                local_tool_workdir=tmp_path,
            )

    with patch("omnigent.runner.tool_dispatch.ToolManager") as manager_type:
        manager_type.return_value.call_tool.side_effect = capture_tool
        assert asyncio.run(dispatch()) == "ok"

    assert [ctx.caller_user_id for ctx in captured_contexts] == ["caller@example.com"]


def test_forward_event_sends_only_authenticated_caller_to_runner(tmp_path: Path) -> None:
    store = SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'forward.db'}")
    conversation = store.create_conversation(workspace=str(tmp_path), title="Existing title")
    posted_bodies = []

    def capture_request(request: httpx.Request) -> httpx.Response:
        posted_bodies.append(json.loads(request.content))
        return httpx.Response(202)

    async def forward(created_by: str | None) -> None:
        async with httpx.AsyncClient(
            base_url="http://runner.test", transport=httpx.MockTransport(capture_request)
        ) as client:
            await _forward_event_to_runner(
                conversation.id,
                conversation,
                SessionEventInput(
                    type="message",
                    data={"role": "user", "content": [{"type": "input_text", "text": "Hello"}]},
                ),
                store,
                client,
                created_by=created_by,
            )

    asyncio.run(forward("caller@example.com"))
    asyncio.run(forward(None))

    assert posted_bodies[0]["caller_user_id"] == "caller@example.com"
    assert "caller_user_id" not in posted_bodies[1]
