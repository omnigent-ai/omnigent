"""Tests for SqlAlchemyAgentStore."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import get_or_create_engine
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def test_create_and_get(agent_store: SqlAlchemyAgentStore) -> None:
    agent = agent_store.create(
        agent_id="88089a8b5dd4eb29fe17d41b2b028cfa",
        name="gpt-4",
        bundle_location="ag_test_gpt4/fakehash",
    )
    assert len(agent.id) == 32
    assert agent.name == "gpt-4"

    fetched = agent_store.get(agent.id)
    assert fetched is not None
    assert fetched.id == agent.id
    assert fetched.name == "gpt-4"


def test_get_nonexistent(agent_store: SqlAlchemyAgentStore) -> None:
    assert agent_store.get("5ff5b2e31fe10beb80134394037b17b0") is None


def test_get_by_name(agent_store: SqlAlchemyAgentStore) -> None:
    agent_store.create(
        agent_id="d949d15d8243d399d68ce236abee269d",
        name="claude",
        bundle_location="ag_test_claude/fakehash",
    )
    found = agent_store.get_by_name("claude")
    assert found is not None
    assert found.name == "claude"
    assert agent_store.get_by_name("missing") is None


def test_create_rejects_duplicate_template_name(agent_store: SqlAlchemyAgentStore) -> None:
    """Template names are unique within a workspace, enforced by the store.

    The DB has no partial unique index (MySQL can't build one), so the store's
    create() is the guard.
    """
    agent_store.create(
        agent_id="7881b57b13260d58fd37b103ee69db65",
        name="dup-name",
        bundle_location="ag_dup_a/fakehash",
    )
    with pytest.raises(IntegrityError):
        agent_store.create(
            agent_id="d924ef7a50570dbbf38c67b059afc7ef",
            name="dup-name",
            bundle_location="ag_dup_b/fakehash",
        )


def test_get_by_name_and_list_hide_session_scoped_agents(
    agent_store: SqlAlchemyAgentStore,
    db_uri: str,
) -> None:
    """Public agent lookup APIs return only template agents."""
    engine = get_or_create_engine(db_uri)
    with engine.begin() as conn:
        # kind moved to omnigent_conversation_metadata; conversations no longer has it.
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id) "
                "VALUES (:id, :ts, :ts, :id)",
            ),
            # Raw SQL bypasses the Uuid16 TypeDecorator, so bind 16 raw bytes;
            # a 32-char hex string overflows the BINARY(16) column on MySQL.
            {"id": bytes.fromhex("3e2c8fc48e056223d18a47a8d4660491"), "ts": 1700000000},
        )
        conn.execute(
            sa.text(
                "INSERT INTO agents "
                "(id, created_at, name, bundle_location, version, kind) "
                "VALUES (:id, :ts, :name, :loc, 1, 2)",  # kind=2 → 'session'
            ),
            {
                "id": bytes.fromhex("6ec5d35246127ba7b23bd47aa95208ec"),
                "ts": 1700000001,
                "name": "session-only-agent",
                "loc": "ag_agent_store_session/bundle",
            },
        )
    template_agent = agent_store.create(
        agent_id="ee6a8659002db5242a56278b73f7a06d",
        name="template-agent",
        bundle_location="ag_agent_store_template/bundle",
    )

    assert agent_store.get_by_name("session-only-agent") is None
    page = agent_store.list(limit=100, order="asc")
    listed_names = [agent.name for agent in page.data]
    assert "session-only-agent" not in listed_names
    assert template_agent.name in listed_names


def test_create_with_description(agent_store: SqlAlchemyAgentStore) -> None:
    agent = agent_store.create(
        agent_id="81b344a1abb05d6096989e2aff583e37",
        name="helper",
        bundle_location="ag_test_helper/fakehash",
        description="A helper agent",
    )
    assert agent.description == "A helper agent"


def test_delete(agent_store: SqlAlchemyAgentStore) -> None:
    agent = agent_store.create(
        agent_id="ef3b53ae229c2fa6af1e4189fa27b74e",
        name="temp",
        bundle_location="ag_test_temp/fakehash",
    )
    assert agent_store.delete(agent.id) is True
    assert agent_store.get(agent.id) is None
    assert agent_store.delete(agent.id) is False


def test_list_pagination(agent_store: SqlAlchemyAgentStore) -> None:
    for i in range(5):
        agent_store.create(
            agent_id=f"{i:032x}", name=f"agent-{i}", bundle_location=f"{i:032x}/fakehash"
        )

    page1 = agent_store.list(limit=2)
    assert len(page1.data) == 2
    assert page1.has_more is True

    page2 = agent_store.list(limit=2, after=page1.last_id)
    assert len(page2.data) == 2
    assert page2.has_more is True

    page3 = agent_store.list(limit=2, after=page2.last_id)
    assert len(page3.data) == 1
    assert page3.has_more is False


def test_list_returns_newest_first(agent_store: SqlAlchemyAgentStore) -> None:
    a1 = agent_store.create(
        agent_id="7dc777d26c3e1b66897f9fa0bf4848fb",
        name="first",
        bundle_location="ag_test_first/fakehash",
    )
    a2 = agent_store.create(
        agent_id="c7ba0ac9893ab0ecef5f36f17b808045",
        name="second",
        bundle_location="ag_test_second/fakehash",
    )
    page = agent_store.list()
    ids = {a.id for a in page.data}
    # Both returned; ordering is (created_at DESC, id DESC) —
    # same-second items are ordered by ID, not insertion order.
    assert ids == {a1.id, a2.id}


def test_list_order_asc(agent_store: SqlAlchemyAgentStore) -> None:
    for i in range(3):
        agent_store.create(
            agent_id=f"{i:032x}", name=f"agent-{i}", bundle_location=f"{i:032x}/fakehash"
        )
    page_desc = agent_store.list(order="desc")
    page_asc = agent_store.list(order="asc")
    assert [a.id for a in page_asc.data] == list(reversed([a.id for a in page_desc.data]))


def test_list_before_cursor(agent_store: SqlAlchemyAgentStore) -> None:
    for i in range(5):
        agent_store.create(
            agent_id=f"{i:032x}", name=f"agent-{i}", bundle_location=f"{i:032x}/fakehash"
        )
    # Paginate with after, then use before on the last page's first item
    # to go backwards and verify no overlap.
    page1 = agent_store.list(limit=3)
    page2 = agent_store.list(limit=3, after=page1.last_id)
    # before the first item of page2 should give us page1's items
    back = agent_store.list(limit=3, before=page2.first_id)
    assert [a.id for a in back.data] == [a.id for a in page1.data]


def test_list_asc_with_after_cursor(agent_store: SqlAlchemyAgentStore) -> None:
    for i in range(5):
        agent_store.create(
            agent_id=f"{i:032x}", name=f"agent-{i}", bundle_location=f"{i:032x}/fakehash"
        )
    page1 = agent_store.list(limit=2, order="asc")
    assert len(page1.data) == 2
    assert page1.has_more is True

    page2 = agent_store.list(limit=2, order="asc", after=page1.last_id)
    assert len(page2.data) == 2
    assert page2.has_more is True

    page3 = agent_store.list(limit=2, order="asc", after=page2.last_id)
    assert len(page3.data) == 1
    assert page3.has_more is False

    # All pages together should equal the full asc listing
    all_ids = [a.id for a in page1.data + page2.data + page3.data]
    full_asc = agent_store.list(limit=100, order="asc")
    assert all_ids == [a.id for a in full_asc.data]


# ── Update tests ───────────────────────────────────────────────


def test_update_agent(agent_store: SqlAlchemyAgentStore) -> None:
    """update() changes bundle_location, bumps version, sets updated_at."""
    agent = agent_store.create(
        agent_id="409a6849f6efefc6ba8da809a29b9b0b",
        name="updatable",
        bundle_location="ag_test_upd/hash1",
    )
    # version=1 and updated_at=None on creation
    assert agent.version == 1
    assert agent.updated_at is None

    updated = agent_store.update("409a6849f6efefc6ba8da809a29b9b0b", "ag_test_upd/hash2")
    assert updated is not None
    assert updated.version == 2
    assert updated.bundle_location == "ag_test_upd/hash2"
    assert updated.updated_at is not None
    # Name stays the same
    assert updated.name == "updatable"


def test_update_nonexistent_agent(agent_store: SqlAlchemyAgentStore) -> None:
    """update() returns None for a nonexistent agent."""
    assert agent_store.update("5ff5b2e31fe10beb80134394037b17b0", "loc") is None


def test_update_increments_version(agent_store: SqlAlchemyAgentStore) -> None:
    """Multiple updates increment version monotonically."""
    agent_store.create(
        agent_id="9dafdd1d4311d9f16337323a6d653308",
        name="versioned",
        bundle_location="ag_test_ver/h1",
    )
    v2 = agent_store.update("9dafdd1d4311d9f16337323a6d653308", "ag_test_ver/h2")
    v3 = agent_store.update("9dafdd1d4311d9f16337323a6d653308", "ag_test_ver/h3")
    assert v2 is not None and v2.version == 2
    assert v3 is not None and v3.version == 3


def test_create_agent_has_version_1(agent_store: SqlAlchemyAgentStore) -> None:
    """Newly created agents start at version 1."""
    agent = agent_store.create(
        agent_id="3b1917f10e098e91d3e2fe6bd30104ef",
        name="fresh",
        bundle_location="ag_test_v1/hash",
    )
    assert agent.version == 1
    assert agent.updated_at is None


# ── get_names tests ───────────────────────────────────────────────


def test_get_names_returns_id_to_name_mapping(agent_store: SqlAlchemyAgentStore) -> None:
    """get_names batch-fetches agent names by ID."""
    agent_store.create(
        agent_id="3f1269c64e8e0dae1e03bd1472ff4d84",
        name="alpha",
        bundle_location="ag_names_a/hash",
    )
    agent_store.create(
        agent_id="1dd1e3e87699fdd5c6d60680291dbaef", name="beta", bundle_location="ag_names_b/hash"
    )
    result = agent_store.get_names(
        ["3f1269c64e8e0dae1e03bd1472ff4d84", "1dd1e3e87699fdd5c6d60680291dbaef"]
    )
    assert result == {
        "3f1269c64e8e0dae1e03bd1472ff4d84": "alpha",
        "1dd1e3e87699fdd5c6d60680291dbaef": "beta",
    }


def test_get_names_omits_missing_ids(agent_store: SqlAlchemyAgentStore) -> None:
    """get_names silently omits IDs not found in the store."""
    agent_store.create(
        agent_id="e78cb9ee170f482daddce8809f06daec",
        name="gamma",
        bundle_location="ag_names_c/hash",
    )
    result = agent_store.get_names(
        ["e78cb9ee170f482daddce8809f06daec", "5ff5b2e31fe10beb80134394037b17b0"]
    )
    assert result == {"e78cb9ee170f482daddce8809f06daec": "gamma"}


def test_get_names_empty_input(agent_store: SqlAlchemyAgentStore) -> None:
    """get_names with empty list returns empty dict without hitting DB."""
    assert agent_store.get_names([]) == {}


# ── list edge cases ───────────────────────────────────────────────


def test_list_empty(agent_store: SqlAlchemyAgentStore) -> None:
    """list on an empty store returns empty PagedList."""
    page = agent_store.list()
    assert page.data == []
    assert page.first_id is None
    assert page.last_id is None
    assert page.has_more is False


def test_delete_nonexistent_returns_false(agent_store: SqlAlchemyAgentStore) -> None:
    """delete returns False for an ID that was never created."""
    result = agent_store.delete("5996d55aa263c10a717e2ee631f46409")
    assert result is False


# ── Session-scoped agent → spawn-tree root resolution ──────────────
#
# Regression for OMNI-1611. Named ``sys_session_send`` children are created
# bound to the *same* agent_id as their mint, so several conversation rows share
# it. ``get()`` resolves ``agent.session_id`` to the agent's spawn-tree ROOT,
# which backs the owning-session auth check in ``validate_session_agent``
# (``check_session_access`` grants on the root's ACL). Every row sharing the
# agent_id carries the same ``root_conversation_id``, so the lookup is a single
# bounded query and returns the same root regardless of which row it reads — no
# "wrong row" to return, and never ``None`` for a session-scoped agent.


def _add_named_child(
    conversation_store: SqlAlchemyConversationStore,
    *,
    agent_id: str,
    parent_conversation_id: str,
    sub_agent_name: str,
) -> str:
    """Create a named sub-agent child bound to the parent's agent_id.

    Mirrors the production named-``sys_session_send`` create, which passes
    ``agent_id=<parent agent>`` and a non-null ``sub_agent_name``.

    :returns: The new child conversation id.
    """
    child = conversation_store.create_conversation(
        kind="sub_agent",
        title=f"{sub_agent_name}:child",
        parent_conversation_id=parent_conversation_id,
        agent_id=agent_id,
        sub_agent_name=sub_agent_name,
    )
    return child.id


def test_session_scoped_agent_resolves_to_root_top_level(
    agent_store: SqlAlchemyAgentStore,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A top-level-minted agent resolves to its own conversation, not a child.

    Guards the original unordered ``LIMIT 1`` bug: after named children share
    the agent_id, the reverse lookup must still return the owning session. For a
    top-level mint the spawn-tree root IS the mint conversation.
    """
    created = conversation_store.create_session_with_agent(
        agent_id="a0000000000000000000000000000001",
        agent_name="orchestrator-top",
        agent_bundle_location="ag_orchestrator_top/bundle",
        agent_description=None,
        title="orchestrator",
    )
    mint_id = created.conversation.id
    _add_named_child(
        conversation_store,
        agent_id=created.agent.id,
        parent_conversation_id=mint_id,
        sub_agent_name="worker_a",
    )
    _add_named_child(
        conversation_store,
        agent_id=created.agent.id,
        parent_conversation_id=mint_id,
        sub_agent_name="worker_b",
    )

    fetched = agent_store.get(created.agent.id)
    assert fetched is not None
    assert fetched.session_id == mint_id  # top-level mint == its own root
    # update() shares the same reverse lookup; it must resolve identically.
    updated = agent_store.update(created.agent.id, "ag_orchestrator_top/bundle2")
    assert updated is not None
    assert updated.session_id == mint_id


def test_session_scoped_agent_resolves_to_root_child_minted(
    agent_store: SqlAlchemyAgentStore,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A child-minted agent resolves to its spawn-tree root, never ``None``.

    This is the case the reverted ``parent_conversation_id IS NULL`` filter got
    wrong: a bundle uploaded as a child has a mint whose own
    ``parent_conversation_id`` is non-null, so that filter matched nothing and
    returned ``None`` — which would skip the owning-session auth check. Resolving
    the shared spawn-tree root returns a real, always-visible ancestor instead.
    """
    parent = conversation_store.create_conversation(
        kind="default",
        title="uploader-parent",
    )
    created = conversation_store.create_session_with_agent(
        agent_id="a0000000000000000000000000000002",
        agent_name="orchestrator-child",
        agent_bundle_location="ag_orchestrator_child/bundle",
        agent_description=None,
        title="child-minted orchestrator",
        parent_conversation_id=parent.id,
    )
    mint_id = created.conversation.id
    # The mint itself is a child (non-null parent), so parent-nullness could not
    # find it; its spawn-tree root is the uploader parent (top-level == own root).
    mint_conv = conversation_store.get_conversation(mint_id)
    assert mint_conv is not None
    assert mint_conv.parent_conversation_id == parent.id
    assert mint_conv.root_conversation_id == parent.id

    _add_named_child(
        conversation_store,
        agent_id=created.agent.id,
        parent_conversation_id=mint_id,
        sub_agent_name="worker_a",
    )
    _add_named_child(
        conversation_store,
        agent_id=created.agent.id,
        parent_conversation_id=mint_id,
        sub_agent_name="worker_b",
    )

    fetched = agent_store.get(created.agent.id)
    assert fetched is not None
    assert fetched.session_id is not None  # the #4501 regression: must not be None
    assert fetched.session_id == parent.id  # the shared spawn-tree root


def test_session_scoped_agent_without_children_resolves_to_root(
    agent_store: SqlAlchemyAgentStore,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The common case — no named children — resolves to the mint (its own root)."""
    created = conversation_store.create_session_with_agent(
        agent_id="a0000000000000000000000000000003",
        agent_name="solo-agent",
        agent_bundle_location="ag_solo/bundle",
        agent_description=None,
        title="solo",
    )
    fetched = agent_store.get(created.agent.id)
    assert fetched is not None
    assert fetched.session_id == created.conversation.id


def test_session_scoped_agent_resolves_to_root_split_db(tmp_path: Path) -> None:
    """Split-DB: the lookup resolves the root when conversations live in the AP DB.

    ``conversations`` (agent_id + root_conversation_id) is on the AP DB while the
    agent row is on the Omnigent DB; the reverse lookup must target the AP engine.
    """
    omnigent_uri = f"sqlite:///{tmp_path}/omnigent.db"
    conv_uri = f"sqlite:///{tmp_path}/conversations.db"
    conversation_store = SqlAlchemyConversationStore(omnigent_uri, conv_uri)

    created = conversation_store.create_session_with_agent(
        agent_id="a0000000000000000000000000000004",
        agent_name="orchestrator-split",
        agent_bundle_location="ag_orchestrator_split/bundle",
        agent_description=None,
        title="split orchestrator",
    )
    mint_id = created.conversation.id
    _add_named_child(
        conversation_store,
        agent_id=created.agent.id,
        parent_conversation_id=mint_id,
        sub_agent_name="worker_a",
    )
    _add_named_child(
        conversation_store,
        agent_id=created.agent.id,
        parent_conversation_id=mint_id,
        sub_agent_name="worker_b",
    )

    agent_store = SqlAlchemyAgentStore(omnigent_uri, conv_uri)
    fetched = agent_store.get(created.agent.id)
    assert fetched is not None
    assert fetched.session_id == mint_id
