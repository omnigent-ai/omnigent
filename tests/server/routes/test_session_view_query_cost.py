"""Database cost and response of the ``GET /v1/sessions/{id}`` session view.

Clients poll this route while a session is open, so every statement and pool
checkout it issues is paid per poll. The cost test pins the statements by
semantic query name; the matrix test pins the response fields those reads feed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from sqlalchemy import event

from omnigent.db.query_context import current_query_name
from omnigent.db.utils import get_or_create_engine
from omnigent.server.auth import LEVEL_MANAGE, LEVEL_READ, RESERVED_USER_PUBLIC
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio

_OWNER = {"X-Forwarded-Email": "owner@example.com"}
_VIEWER = {"X-Forwarded-Email": "viewer@example.com"}
# The web chat hydrates items and liveness through other routes.
_WEB_VIEW_PARAMS = {"include_items": "false", "include_liveness": "false"}


@dataclass
class _DatabaseCost:
    """Named statements and pool checkouts observed on the app's engine."""

    query_names: list[str | None] = field(default_factory=list)
    checkouts: int = 0

    def on_statement(
        self,
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        # SQLite connection setup is not application work.
        if not statement.lstrip().upper().startswith("PRAGMA"):
            self.query_names.append(current_query_name())

    def on_checkout(self, *_args: Any) -> None:
        self.checkouts += 1

    def reset(self) -> None:
        self.query_names.clear()
        self.checkouts = 0


@pytest.fixture()
def database_cost(db_uri: str) -> Iterator[_DatabaseCost]:
    engine = get_or_create_engine(db_uri)
    cost = _DatabaseCost()
    event.listen(engine, "before_cursor_execute", cost.on_statement)
    event.listen(engine, "checkout", cost.on_checkout)
    try:
        yield cost
    finally:
        event.remove(engine, "before_cursor_execute", cost.on_statement)
        event.remove(engine, "checkout", cost.on_checkout)


async def _create_session(client: httpx.AsyncClient, metadata: dict[str, Any]) -> str:
    response = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("agent.tar.gz", build_agent_bundle(name="viewer"), "application/gzip")},
        headers=_OWNER,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


@dataclass(frozen=True)
class _Sessions:
    plain: str
    rich: str
    child: str
    side_chat: str
    archived: str


async def _seed_sessions(client: httpx.AsyncClient, db_uri: str) -> _Sessions:
    """Seed labelled, usage-bearing, runner-bound and shared sessions plus sub-sessions."""
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    plain = await _create_session(client, {})
    rich = await _create_session(
        client, {"title": "Rich", "labels": {"env": "test", "team": "core"}}
    )
    rich_conversation = store.get_conversation(rich)
    assert rich_conversation is not None
    agent_id = rich_conversation.agent_id
    child = store.create_conversation(
        kind="sub_agent", title="worker:one", parent_conversation_id=rich, agent_id=agent_id
    ).id
    side_chat = store.create_conversation(
        kind="sub_agent",
        title="Side chat",
        parent_conversation_id=rich,
        side_chat_parent_id=rich,
        agent_id=agent_id,
        labels={"omnigent.side_chat": "1"},
    ).id
    store.set_session_usage(rich, {"input_tokens": 1, "output_tokens": 2, "total_cost_usd": 0.25})
    store.set_session_usage(child, {"input_tokens": 7, "output_tokens": 3, "total_cost_usd": 0.5})
    store.set_runner_id(rich, "runner_view")
    archived = await _create_session(client, {"title": "Old"})
    store.update_conversation(archived, archived=True)
    permissions.grant(RESERVED_USER_PUBLIC, rich, LEVEL_READ)
    permissions.grant("viewer@example.com", plain, LEVEL_MANAGE)
    return _Sessions(plain, rich, child, side_chat, archived)


_ACCESS_READ_NAMES = [
    "omnigent.permission_store.resolve_access",
    "omnigent.permission_store.resolve_access",
    "omnigent.conversation_store.select_conversation_with_labels_by_id",
    "omnigent.conversation_store.select_conversation_metadata_by_id",
]
_AGENT_READ_NAMES = [
    "omnigent.agent_store.select_agent_by_id",
    "omnigent.agent_store.select_session_id_for_agent",
]
_USAGE_READ_NAMES = [
    "omnigent.conversation_store.list_conversations",
    "omnigent.conversation_store.list_conversations",
    "omnigent.conversation_store.list_conversations",
]


async def test_session_view_reads_each_record_once(
    db_uri: str,
    auth_client: httpx.AsyncClient,
    database_cost: _DatabaseCost,
) -> None:
    """One view request reads each record once, in one checkout per read burst.

    Before: 19 statements in 13 checkouts for the full view (the agent was read
    three times, labels and the owner grant separately), 13 in 10 for the web view.
    """
    sessions = await _seed_sessions(auth_client, db_uri)
    database_cost.reset()

    response = await auth_client.get(f"/v1/sessions/{sessions.rich}", headers=_OWNER)

    assert response.status_code == 200, response.text
    assert database_cost.query_names == [
        *_ACCESS_READ_NAMES,
        "omnigent.conversation_store.list_items",
        *_AGENT_READ_NAMES,
        "omnigent.conversation_store.get_session_connectivity",
        "omnigent.conversation_store.get_session_connectivity",
        *_USAGE_READ_NAMES,
    ]
    assert database_cost.checkouts == 3

    database_cost.reset()
    response = await auth_client.get(
        f"/v1/sessions/{sessions.rich}", params=_WEB_VIEW_PARAMS, headers=_OWNER
    )

    assert response.status_code == 200, response.text
    # The permission store serves this caller's repeated resolve from its cache.
    assert database_cost.query_names == [
        *_ACCESS_READ_NAMES[2:],
        *_AGENT_READ_NAMES,
        *_USAGE_READ_NAMES,
    ]
    assert database_cost.checkouts == 3


@pytest.mark.parametrize("params", [{}, _WEB_VIEW_PARAMS], ids=["full", "web"])
async def test_session_view_response_matrix(
    db_uri: str,
    auth_client: httpx.AsyncClient,
    params: dict[str, str],
) -> None:
    """Access, labels, ownership, usage and agent fields for each session shape."""
    sessions = await _seed_sessions(auth_client, db_uri)
    agent_fields = {"agent_name": "viewer", "harness": "claude-sdk"}
    expected: list[tuple[str, dict[str, str], dict[str, Any]]] = [
        (
            sessions.plain,
            _OWNER,
            {"permission_level": 4, "side_chat_allowed": True, "total_cost_usd": None},
        ),
        (sessions.plain, _VIEWER, {"permission_level": 3, "side_chat_allowed": False}),
        (
            sessions.rich,
            _OWNER,
            {
                "title": "Rich",
                "labels": {"env": "test", "omnigent.client_surface": "cli", "team": "core"},
                "runner_id": "runner_view",
                "permission_level": 4,
                "side_chat_allowed": True,
                "total_cost_usd": 0.75,
            },
        ),
        (sessions.rich, _VIEWER, {"permission_level": 1, "side_chat_allowed": False}),
        (
            sessions.child,
            _OWNER,
            {"labels": {}, "permission_level": None, "side_chat_allowed": True},
        ),
        (sessions.child, _VIEWER, {"side_chat_allowed": False, "total_cost_usd": 0.5}),
        (
            sessions.side_chat,
            _OWNER,
            {"labels": {"omnigent.side_chat": "1"}, "side_chat_allowed": True},
        ),
        (sessions.archived, _OWNER, {"archived": True, "side_chat_allowed": True}),
    ]
    for session_id, headers, fields in expected:
        response = await auth_client.get(
            f"/v1/sessions/{session_id}", params=params, headers=headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        observed = {key: body[key] for key in [*fields, *agent_fields]}
        assert observed == {**fields, **agent_fields}, (session_id, headers)

    # Visibility is unchanged: a session the caller cannot read stays absent.
    response = await auth_client.get(
        f"/v1/sessions/{sessions.archived}", params=params, headers=_VIEWER
    )
    assert response.status_code == 404
