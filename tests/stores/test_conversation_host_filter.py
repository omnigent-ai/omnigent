"""Host filtering must preserve pagination and isolation in both DB layouts."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

import pytest
from sqlalchemy import event, update

from omnigent.db.db_models import SqlConversation, workspace_scope
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


@pytest.fixture(params=["conversation_store", "split_db_conversation_store"])
def store(request: pytest.FixtureRequest) -> SqlAlchemyConversationStore:
    return request.getfixturevalue(request.param)


@pytest.mark.parametrize("dashed", [False, True])
def test_host_filter_uses_persisted_binding(
    store: SqlAlchemyConversationStore, dashed: bool
) -> None:
    host = uuid4()
    bound = store.create_conversation(host_id=host.hex, workspace="/workspace")
    other = store.create_conversation(host_id=uuid4().hex, workspace="/workspace")
    unbound = store.create_conversation()

    # No host registry entry is required; an offline host is still filterable.
    page = store.list_conversations(host_id=str(host) if dashed else host.hex)
    assert [conv.id for conv in page.data] == [bound.id]
    assert not page.has_more
    assert {conv.id for conv in store.list_conversations().data} == {
        bound.id,
        other.id,
        unbound.id,
    }
    empty = store.list_conversations(host_id=uuid4().hex)
    assert empty.data == []
    assert not empty.has_more
    assert empty.first_id is None
    assert empty.last_id is None


@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("sort_by", ["created_at", "updated_at"])
def test_host_filter_applies_before_pagination(
    store: SqlAlchemyConversationStore, order: str, sort_by: str
) -> None:
    host_id = uuid4().hex
    matching = []
    for index in range(7):
        conv = store.create_conversation(
            host_id=host_id if index % 2 else uuid4().hex,
            workspace="/workspace",
        )
        with store._conv_session("seed_host_filter_sort_order") as session:
            session.execute(
                update(SqlConversation)
                .where(SqlConversation.workspace_id == 0, SqlConversation.id == conv.id)
                .values(created_at=index, updated_at=10 - index)
            )
        if index % 2:
            matching.append(conv.id)
    if (order == "desc") != (sort_by == "updated_at"):
        matching.reverse()

    first = store.list_conversations(host_id=host_id, limit=2, order=order, sort_by=sort_by)
    assert [conv.id for conv in first.data] == matching[:2]
    assert first.first_id == matching[0]
    assert first.last_id == matching[1]
    assert first.has_more
    second = store.list_conversations(
        host_id=host_id, limit=2, after=first.last_id, order=order, sort_by=sort_by
    )
    assert [conv.id for conv in second.data] == matching[2:]
    assert not second.has_more
    previous = store.list_conversations(
        host_id=host_id, limit=2, before=second.first_id, order=order, sort_by=sort_by
    )
    assert [conv.id for conv in previous.data] == matching[:2]


def test_host_filter_composes_with_existing_filters(store: SqlAlchemyConversationStore) -> None:
    host_id, agent_id = uuid4().hex, uuid4().hex
    active = store.create_conversation(
        title="needle active",
        agent_id=agent_id,
        host_id=host_id,
        workspace="/workspace",
        labels={"omni_project": "example"},
    )
    archived = store.create_conversation(
        title="needle archived",
        agent_id=agent_id,
        host_id=host_id,
        workspace="/workspace",
        labels={"omni_project": "example"},
    )
    store.update_conversation(archived.id, archived=True)
    store.create_conversation(
        title="different title", agent_id=agent_id, host_id=host_id, workspace="/workspace"
    )
    store.create_conversation(
        title="needle wrong host",
        agent_id=agent_id,
        host_id=uuid4().hex,
        workspace="/workspace",
        labels={"omni_project": "example"},
    )
    store.create_conversation(
        title="needle wrong agent",
        agent_id=uuid4().hex,
        host_id=host_id,
        workspace="/workspace",
        labels={"omni_project": "example"},
    )
    page = store.list_conversations(
        host_id=host_id, agent_id=agent_id, project="example", search_query="needle"
    )
    assert [conv.id for conv in page.data] == [active.id]
    page = store.list_conversations(
        host_id=host_id,
        agent_id=agent_id,
        project="example",
        search_query="needle",
        include_archived=True,
    )
    assert {conv.id for conv in page.data} == {active.id, archived.id}


@pytest.mark.parametrize("visibility", ["all", "mine", "shared"])
def test_host_filter_preserves_permissions(
    store: SqlAlchemyConversationStore, visibility: Literal["all", "mine", "shared"]
) -> None:
    permissions = SqlAlchemyPermissionStore(
        store._engine.url.render_as_string(hide_password=False)
    )
    alice, bob = "alice@example.com", "bob@example.com"
    for user in (alice, bob):
        permissions.ensure_user(user)
    host_id = uuid4().hex
    owned, shared, private, other_host = [
        store.create_conversation(
            host_id=host_id if index < 3 else uuid4().hex, workspace="/workspace"
        )
        for index in range(4)
    ]
    permissions.grant(alice, owned.id, LEVEL_OWNER)
    permissions.grant(bob, shared.id, LEVEL_OWNER)
    permissions.grant(alice, shared.id, LEVEL_READ)
    permissions.grant(bob, private.id, LEVEL_OWNER)
    permissions.grant(alice, other_host.id, LEVEL_OWNER)

    page = store.list_conversations(
        host_id=host_id,
        accessible_by=alice,
        owned_by=alice if visibility == "mine" else None,
        shared_only=visibility == "shared",
    )
    expected = {"all": {owned.id, shared.id}, "mine": {owned.id}, "shared": {shared.id}}
    assert {conv.id for conv in page.data} == expected[visibility]
    assert not page.has_more


def test_host_filter_respects_workspace_scope(store: SqlAlchemyConversationStore) -> None:
    host_id, session_id = uuid4().hex, uuid4().hex
    with workspace_scope(42):
        store.create_conversation(
            conversation_id=session_id, host_id=host_id, workspace="/workspace"
        )
    with workspace_scope(43):
        store.create_conversation(
            conversation_id=session_id, host_id=uuid4().hex, workspace="/workspace"
        )
        assert store.list_conversations(host_id=host_id).data == []
        bound = store.create_conversation(host_id=host_id, workspace="/workspace")
        assert [conv.id for conv in store.list_conversations(host_id=host_id).data] == [bound.id]
    with workspace_scope(42):
        assert [conv.id for conv in store.list_conversations(host_id=host_id).data] == [session_id]


def test_single_db_pushes_host_filter_into_paged_query(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    host_id = uuid4().hex
    conv = conversation_store.create_conversation(host_id=host_id, workspace="/workspace")
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(conversation_store._engine, "before_cursor_execute", capture)
    try:
        page = conversation_store.list_conversations(host_id=host_id, limit=1)
    finally:
        event.remove(conversation_store._engine, "before_cursor_execute", capture)
    assert [item.id for item in page.data] == [conv.id]
    host_queries = [sql for sql in statements if "host_id =" in sql]
    assert len(host_queries) == 1
    assert "exists" in host_queries[0]
    assert "limit" in host_queries[0]
