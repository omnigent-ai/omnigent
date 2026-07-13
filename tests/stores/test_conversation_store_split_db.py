"""Tests for SqlAlchemyConversationStore in split-DB mode.

Exercises the same operations as test_conversation_store.py but with the
Omnigent DB and AP DB backed by two separate SQLite files, verifying that
rows land in the right database.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest.fixture()
def omnigent_db(tmp_path: Path) -> Path:
    return tmp_path / "omnigent.db"


@pytest.fixture()
def conv_db(tmp_path: Path) -> Path:
    return tmp_path / "conversations.db"


@pytest.fixture()
def store(omnigent_db: Path, conv_db: Path) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(
        f"sqlite:///{omnigent_db}",
        f"sqlite:///{conv_db}",
    )


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(str(db)) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _count(db: Path, table: str) -> int:
    with sqlite3.connect(str(db)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _col(db: Path, table: str, col: str, where: str = "") -> list:
    with sqlite3.connect(str(db)) as conn:
        q = f"SELECT {col} FROM {table}" + (f" WHERE {where}" if where else "")
        return [r[0] for r in conn.execute(q).fetchall()]


# ── Table placement ────────────────────────────────────


def test_tables_live_in_correct_db(
    omnigent_db: Path,
    conv_db: Path,
    store: SqlAlchemyConversationStore,  # triggers DB init
) -> None:
    del store  # only used to initialise both databases
    omnigent_tables = _tables(omnigent_db)
    conv_tables = _tables(conv_db)

    # AP tables in conv_db only
    for t in ("conversations", "conversation_items", "conversation_labels"):
        assert t in conv_tables, f"{t} missing from conv_db"

    # Omnigent tables in omnigent_db
    for t in ("omnigent_conversation_metadata", "agents", "hosts", "policies", "comments"):
        assert t in omnigent_tables, f"{t} missing from omnigent_db"

    # AP tables must NOT appear in omnigent_db (no schema migration runs there)
    assert "omnigent_conversation_metadata" not in conv_tables


# ── create_conversation ────────────────────────────────


def test_create_conversation_rows_land_in_correct_db(
    omnigent_db: Path, conv_db: Path, store: SqlAlchemyConversationStore
) -> None:
    store.create_conversation(
        kind="default",
        title="hello",
        runner_id="runner_abc",
        workspace="/tmp/proj",
    )

    # AP DB: title, agent binding
    assert _count(conv_db, "conversations") == 1
    assert _col(conv_db, "conversations", "title") == ["hello"]

    # Omnigent DB: operational fields
    assert _count(omnigent_db, "omnigent_conversation_metadata") == 1
    assert _col(omnigent_db, "omnigent_conversation_metadata", "runner_id") == ["runner_abc"]
    assert _col(omnigent_db, "omnigent_conversation_metadata", "workspace") == ["/tmp/proj"]


def test_create_sub_agent_conversation(
    omnigent_db: Path, conv_db: Path, store: SqlAlchemyConversationStore
) -> None:
    parent = store.create_conversation(kind="default", title="parent")
    child = store.create_conversation(
        kind="sub_agent",
        title="child",
        parent_conversation_id=parent.id,
        sub_agent_name="summarizer",
    )

    assert child.kind == "sub_agent"
    assert child.parent_conversation_id == parent.id
    # kind lives in metadata
    kind_code = _col(omnigent_db, "omnigent_conversation_metadata", "kind", f"id='{child.id}'")
    assert kind_code == [2]
    # title and parent link live in AP
    parent_id_col = _col(conv_db, "conversations", "parent_conversation_id", f"id='{child.id}'")
    assert parent_id_col == [parent.id]


# ── get / list ─────────────────────────────────────────


def test_get_conversation_merges_both_dbs(store: SqlAlchemyConversationStore) -> None:
    conv = store.create_conversation(kind="default", title="merge-test", workspace="/x")
    fetched = store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.title == "merge-test"
    assert fetched.workspace == "/x"
    assert fetched.kind == "default"


def test_get_conversations_bulk(store: SqlAlchemyConversationStore) -> None:
    a = store.create_conversation(title="a")
    b = store.create_conversation(title="b")
    result = store.get_conversations([a.id, b.id])
    assert set(result.keys()) == {a.id, b.id}
    assert result[a.id].title == "a"
    assert result[b.id].title == "b"


def test_list_conversations_kind_filter_crosses_dbs(store: SqlAlchemyConversationStore) -> None:
    store.create_conversation(kind="default", title="top")
    parent = store.create_conversation(kind="default", title="parent2")
    store.create_conversation(kind="sub_agent", title="child", parent_conversation_id=parent.id)

    defaults = store.list_conversations(kind="default")
    subs = store.list_conversations(kind="sub_agent")
    assert all(c.kind == "default" for c in defaults.data)
    assert all(c.kind == "sub_agent" for c in subs.data)


def test_list_conversations_archived_filter(store: SqlAlchemyConversationStore) -> None:
    conv = store.create_conversation(title="to-archive")
    store.update_conversation(conv.id, archived=True)

    active = store.list_conversations(include_archived=False)
    all_ = store.list_conversations(include_archived=True)
    assert all(not c.archived for c in active.data)
    assert any(c.archived for c in all_.data)


# ── labels ─────────────────────────────────────────────


def test_labels_land_in_conv_db(conv_db: Path, store: SqlAlchemyConversationStore) -> None:
    conv = store.create_conversation(title="labeled")
    store.set_labels(conv.id, {"env": "test", "owner": "alice"})

    assert _count(conv_db, "conversation_labels") == 2
    fetched = store.get_conversation(conv.id)
    assert fetched.labels == {"env": "test", "owner": "alice"}


def test_delete_label(store: SqlAlchemyConversationStore) -> None:
    conv = store.create_conversation(title="label-del")
    store.set_labels(conv.id, {"k": "v"})
    store.delete_label(conv.id, "k")
    fetched = store.get_conversation(conv.id)
    assert "k" not in fetched.labels


# ── metadata writes ────────────────────────────────────


def test_set_runner_id_lands_in_omnigent_db(
    omnigent_db: Path, store: SqlAlchemyConversationStore
) -> None:
    conv = store.create_conversation(title="runner")
    store.set_runner_id(conv.id, "runner_xyz")
    runner_ids = _col(
        omnigent_db, "omnigent_conversation_metadata", "runner_id", f"id='{conv.id}'"
    )
    assert runner_ids == ["runner_xyz"]


def test_set_session_state_lands_in_omnigent_db(
    omnigent_db: Path, store: SqlAlchemyConversationStore
) -> None:
    conv = store.create_conversation(title="state")
    store.set_session_state(conv.id, {"counter": 1})
    fetched = store.get_conversation(conv.id)
    assert fetched.session_state == {"counter": 1}


def test_increment_session_usage(store: SqlAlchemyConversationStore) -> None:
    conv = store.create_conversation(title="usage")
    result = store.increment_session_usage(conv.id, {"input_tokens": 100, "total_cost_usd": 0.01})
    assert result["input_tokens"] == 100
    result2 = store.increment_session_usage(conv.id, {"input_tokens": 50})
    assert result2["input_tokens"] == 150


def test_set_external_session_id(store: SqlAlchemyConversationStore) -> None:
    conv = store.create_conversation(title="ext")
    updated = store.set_external_session_id(conv.id, "ext-uuid-123")
    assert updated.external_session_id == "ext-uuid-123"


# ── conversation items ─────────────────────────────────


def test_append_and_list_items_land_in_conv_db(
    conv_db: Path, store: SqlAlchemyConversationStore
) -> None:
    from omnigent.entities import NewConversationItem
    from omnigent.entities.conversation import MessageData

    conv = store.create_conversation(title="items")
    items = store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
            )
        ],
    )
    assert len(items) == 1
    assert _count(conv_db, "conversation_items") == 1

    listed = store.list_items(conv.id)
    assert len(listed.data) == 1


# ── delete ─────────────────────────────────────────────


def test_delete_conversation_cleans_both_dbs(
    omnigent_db: Path, conv_db: Path, store: SqlAlchemyConversationStore
) -> None:
    from omnigent.entities import NewConversationItem
    from omnigent.entities.conversation import MessageData

    conv = store.create_conversation(title="to-delete", runner_id="r1")
    store.set_labels(conv.id, {"k": "v"})
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "bye"}]),
            )
        ],
    )
    assert _count(conv_db, "conversations") == 1
    assert _count(omnigent_db, "omnigent_conversation_metadata") == 1

    deleted = asyncio.run(store.delete_conversation(conv.id))
    assert deleted is True
    assert _count(conv_db, "conversations") == 0
    assert _count(conv_db, "conversation_items") == 0
    assert _count(conv_db, "conversation_labels") == 0
    assert _count(omnigent_db, "omnigent_conversation_metadata") == 0


def test_delete_conversation_subtree_cleans_both_dbs(
    omnigent_db: Path, conv_db: Path, store: SqlAlchemyConversationStore
) -> None:
    parent = store.create_conversation(title="parent")
    store.create_conversation(kind="sub_agent", title="child", parent_conversation_id=parent.id)
    assert _count(conv_db, "conversations") == 2
    assert _count(conv_db, "agent_configuration") == 2
    assert _count(omnigent_db, "omnigent_conversation_metadata") == 2

    asyncio.run(store.delete_conversation(parent.id))
    assert _count(conv_db, "conversations") == 0
    assert _count(conv_db, "agent_configuration") == 0
    assert _count(omnigent_db, "omnigent_conversation_metadata") == 0


# ── get_runner_ids / get_session_connectivity ──────────


def test_get_runner_ids_reads_from_omnigent_db(store: SqlAlchemyConversationStore) -> None:
    a = store.create_conversation(title="a", runner_id="runner_a")
    b = store.create_conversation(title="b")
    ids = store.get_runner_ids([a.id, b.id])
    assert ids[a.id] == "runner_a"
    assert ids[b.id] is None


def test_list_conversations_by_runner_id(store: SqlAlchemyConversationStore) -> None:
    a = store.create_conversation(title="a", runner_id="runner_x")
    store.create_conversation(title="b", runner_id="runner_y")
    results = store.list_conversations_by_runner_id("runner_x")
    assert len(results) == 1
    assert results[0].id == a.id
    assert results[0].title == "a"


# ── fork_conversation ──────────────────────────────────


def test_fork_conversation_copies_to_both_dbs(
    omnigent_db: Path, conv_db: Path, store: SqlAlchemyConversationStore
) -> None:
    from omnigent.entities import NewConversationItem
    from omnigent.entities.conversation import MessageData

    source = store.create_conversation(title="source", workspace="/src")
    store.append(
        source.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "original"}]
                ),
            )
        ],
    )

    fork = store.fork_conversation(source.id, title="fork")
    assert fork.id != source.id
    assert fork.title == "fork"

    assert _count(conv_db, "conversations") == 2
    assert _count(omnigent_db, "omnigent_conversation_metadata") == 2
    assert _count(conv_db, "conversation_items") == 2  # original + copy


# ── AgentStore cross-DB session_id resolution ────────────────────────


def test_agent_store_resolves_session_id_across_dbs(
    omnigent_db: Path,
    conv_db: Path,
    store: SqlAlchemyConversationStore,
) -> None:
    """
    ``agent.session_id`` requires a reverse lookup on
    ``conversations.agent_id``, which lives in the AP DB. An AgentStore
    wired only to the Omnigent DB would query the wrong database and
    silently return ``session_id=None`` for every session-scoped agent.
    """
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    created = store.create_session_with_agent(
        agent_id="ag_split_1",
        agent_name="session-agent",
        agent_bundle_location="ag_split_1/bundle",
        agent_description=None,
        title="split session",
    )
    # Agent row lands in the Omnigent DB; the binding in the AP DB's
    # agent_configuration table.
    assert _count(omnigent_db, "agents") == 1
    assert _col(conv_db, "agent_configuration", "agent_id") == ["ag_split_1"]

    agent_store = SqlAlchemyAgentStore(
        f"sqlite:///{omnigent_db}",
        f"sqlite:///{conv_db}",
    )
    agent = agent_store.get("ag_split_1")
    assert agent is not None
    assert agent.session_id == created.conversation.id

    updated = agent_store.update("ag_split_1", "ag_split_1/bundle2")
    assert updated is not None
    assert updated.session_id == created.conversation.id


# ── Orphan repair: update with a missing metadata row ─────────────────


def test_update_conversation_repairs_missing_metadata(
    omnigent_db: Path,
    conv_db: Path,
    store: SqlAlchemyConversationStore,
) -> None:
    """
    A crash between the AP and metadata transactions during creation
    leaves a conversation with no metadata row. An archive update must
    recreate the row (deriving ``kind`` from the parent pointer) rather
    than silently dropping the write and reporting ``archived=False``.
    """
    parent = store.create_conversation(title="orphan parent")
    child = store.create_conversation(
        kind="sub_agent",
        title="orphan child",
        parent_conversation_id=parent.id,
    )
    # Simulate the creation crash: drop both metadata rows directly.
    with sqlite3.connect(str(omnigent_db)) as conn:
        conn.execute(
            "DELETE FROM omnigent_conversation_metadata WHERE id IN (?, ?)",
            (parent.id, child.id),
        )
        conn.commit()
    assert _count(omnigent_db, "omnigent_conversation_metadata") == 0

    updated = store.update_conversation(parent.id, archived=True)
    assert updated is not None
    assert updated.archived is True

    child_updated = store.update_conversation(child.id, archived=True)
    assert child_updated is not None
    assert child_updated.archived is True
    # kind is rederived from the parent pointer during repair.
    assert child_updated.kind == "sub_agent"

    # Both metadata rows were recreated in the Omnigent DB.
    assert sorted(_col(omnigent_db, "omnigent_conversation_metadata", "id")) == sorted(
        [parent.id, child.id]
    )


# ── Session-scoped agent cleanup on conversation delete ───────────────


def test_delete_conversation_deletes_session_scoped_agent(
    omnigent_db: Path,
    conv_db: Path,
    store: SqlAlchemyConversationStore,
) -> None:
    """Deleting a session deletes the session-scoped agent row backing it."""
    created = store.create_session_with_agent(
        agent_id="ag_del_1",
        agent_name="del-agent",
        agent_bundle_location="ag_del_1/bundle",
        agent_description=None,
        title="del session",
    )
    assert _count(omnigent_db, "agents") == 1

    asyncio.run(store.delete_conversation(created.conversation.id))
    assert _count(omnigent_db, "agents") == 0
    assert _count(conv_db, "agent_configuration") == 0


def test_delete_conversation_keeps_template_agent(
    omnigent_db: Path,
    conv_db: Path,
    store: SqlAlchemyConversationStore,
) -> None:
    """Template agents are shared; deleting a bound session must not delete them."""
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    agent_store = SqlAlchemyAgentStore(
        f"sqlite:///{omnigent_db}",
        f"sqlite:///{conv_db}",
    )
    template = agent_store.create("ag_tmpl_1", "shared-template", "ag_tmpl_1/bundle")
    conv = store.create_conversation(title="uses template", agent_id=template.id)

    asyncio.run(store.delete_conversation(conv.id))
    assert _col(omnigent_db, "agents", "id") == ["ag_tmpl_1"]
