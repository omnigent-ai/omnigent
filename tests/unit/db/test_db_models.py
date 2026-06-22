"""Tests for SQLAlchemy ORM models (omnigent/db/db_models.py).

Verifies that each ORM model can be instantiated, persisted, read back,
and that relationships, defaults, nullable columns, and constraints
behave as expected.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import (
    SqlAccountToken,
    SqlAgent,
    SqlComment,
    SqlConversation,
    SqlConversationItem,
    SqlConversationLabel,
    SqlFile,
    SqlHost,
    SqlPolicy,
    SqlSessionPermission,
    SqlUser,
    SqlUserDailyCost,
)
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker

# ── helpers ───────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _make_agent(
    id: str = "ag_test1",
    name: str = "test-agent",
    session_id: str | None = None,
) -> SqlAgent:
    return SqlAgent(
        id=id,
        created_at=_now(),
        name=name,
        bundle_location="ag_test1/abc123",
        version=1,
        session_id=session_id,
    )


def _make_conversation(
    id: str = "conv_test1",
    agent_id: str | None = None,
    parent_conversation_id: str | None = None,
    root_conversation_id: str | None = None,
    kind: str = "default",
    title: str | None = None,
) -> SqlConversation:
    return SqlConversation(
        id=id,
        created_at=_now(),
        updated_at=_now(),
        kind=kind,
        agent_id=agent_id,
        parent_conversation_id=parent_conversation_id,
        root_conversation_id=root_conversation_id or id,
        title=title,
    )


def _make_item(
    id: str = "msg_test1",
    conversation_id: str = "conv_test1",
    position: int = 0,
) -> SqlConversationItem:
    return SqlConversationItem(
        id=id,
        conversation_id=conversation_id,
        response_id="resp_test1",
        created_at=_now(),
        status="completed",
        position=position,
        type="message",
        data='{"content": [{"type": "text", "text": "hello"}]}',
        search_text="hello",
    )


# ── SqlAgent ──────────────────────────────────────────


class TestSqlAgent:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        agent = _make_agent()
        with managed() as session:
            session.add(agent)

        with managed() as session:
            loaded = session.get(SqlAgent, "ag_test1")
            assert loaded is not None
            assert loaded.name == "test-agent"
            assert loaded.version == 1
            assert loaded.description is None
            assert loaded.updated_at is None
            assert loaded.session_id is None

    def test_nullable_columns(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        agent = _make_agent()
        agent.description = "A test agent"
        agent.updated_at = _now()
        with managed() as session:
            session.add(agent)

        with managed() as session:
            loaded = session.get(SqlAgent, "ag_test1")
            assert loaded is not None
            assert loaded.description == "A test agent"
            assert loaded.updated_at is not None

    def test_session_scoped_agent_fk(self, db_uri: str) -> None:
        """session_id FK to conversations must be valid."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        with managed() as session:
            session.add(conv)

        agent = _make_agent(session_id="conv_test1")
        with managed() as session:
            session.add(agent)

        with managed() as session:
            loaded = session.get(SqlAgent, "ag_test1")
            assert loaded is not None
            assert loaded.session_id == "conv_test1"

    def test_unique_session_id_index(self, db_uri: str) -> None:
        """ix_agents_session_id is unique -- two agents cannot share the same session_id."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        a1 = _make_agent(id="ag_1", name="agent-1", session_id="conv_test1")
        a2 = _make_agent(id="ag_2", name="agent-2", session_id="conv_test1")

        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(conv)
                session.add(a1)
                session.add(a2)


# ── SqlFile ───────────────────────────────────────────


class TestSqlFile:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        f = SqlFile(
            id="file_test1",
            created_at=_now(),
            filename="report.pdf",
            bytes=12345,
            content_type="application/pdf",
        )
        with managed() as session:
            session.add(f)

        with managed() as session:
            loaded = session.get(SqlFile, "file_test1")
            assert loaded is not None
            assert loaded.filename == "report.pdf"
            assert loaded.bytes == 12345
            assert loaded.content_type == "application/pdf"

    def test_nullable_content_type(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        f = SqlFile(
            id="file_test2",
            created_at=_now(),
            filename="data.bin",
            bytes=100,
        )
        with managed() as session:
            session.add(f)

        with managed() as session:
            loaded = session.get(SqlFile, "file_test2")
            assert loaded is not None
            assert loaded.content_type is None


# ── SqlUser ───────────────────────────────────────────


class TestSqlUser:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        user = SqlUser(id="alice@example.com", is_admin=False)
        with managed() as session:
            session.add(user)

        with managed() as session:
            loaded = session.get(SqlUser, "alice@example.com")
            assert loaded is not None
            assert loaded.is_admin is False
            assert loaded.password_hash is None
            assert loaded.created_at is None

    def test_admin_user(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        user = SqlUser(
            id="admin@example.com",
            is_admin=True,
            password_hash="$argon2id$hash",
            created_at=_now(),
        )
        with managed() as session:
            session.add(user)

        with managed() as session:
            loaded = session.get(SqlUser, "admin@example.com")
            assert loaded is not None
            assert loaded.is_admin is True
            assert loaded.password_hash == "$argon2id$hash"

    def test_duplicate_id_raises(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        with managed() as session:
            session.add(SqlUser(id="dup@example.com", is_admin=False))

        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(SqlUser(id="dup@example.com", is_admin=True))


# ── SqlAccountToken ───────────────────────────────────


class TestSqlAccountToken:
    def test_persist_invite_token(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        token = SqlAccountToken(
            id="tok_invite_abc",
            kind="invite",
            created_at=now,
            expires_at=now + 3600,
            created_by="admin@example.com",
            invited_is_admin=True,
        )
        with managed() as session:
            session.add(token)

        with managed() as session:
            loaded = session.get(SqlAccountToken, "tok_invite_abc")
            assert loaded is not None
            assert loaded.kind == "invite"
            assert loaded.user_id is None
            assert loaded.redeemed_at is None
            assert loaded.invited_is_admin is True

    def test_persist_magic_token(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        token = SqlAccountToken(
            id="tok_magic_xyz",
            kind="magic",
            user_id="alice@example.com",
            created_at=now,
            expires_at=now + 300,
        )
        with managed() as session:
            session.add(token)

        with managed() as session:
            loaded = session.get(SqlAccountToken, "tok_magic_xyz")
            assert loaded is not None
            assert loaded.kind == "magic"
            assert loaded.user_id == "alice@example.com"

    def test_check_constraint_rejects_invalid_kind(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        token = SqlAccountToken(
            id="tok_bad",
            kind="invalid",
            created_at=now,
            expires_at=now + 3600,
        )
        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(token)


# ── SqlConversation ───────────────────────────────────


class TestSqlConversation:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation(title="Hello World")
        with managed() as session:
            session.add(conv)

        with managed() as session:
            loaded = session.get(SqlConversation, "conv_test1")
            assert loaded is not None
            assert loaded.title == "Hello World"
            assert loaded.kind == "default"
            assert loaded.archived is False

    def test_defaults(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        with managed() as session:
            session.add(conv)

        with managed() as session:
            loaded = session.get(SqlConversation, "conv_test1")
            assert loaded is not None
            assert loaded.runner_id is None
            assert loaded.host_id is None
            assert loaded.reasoning_effort is None
            assert loaded.model_override is None
            assert loaded.external_session_id is None
            assert loaded.workspace is None
            assert loaded.git_branch is None
            assert loaded.session_state is None
            assert loaded.session_usage is None

    def test_check_constraint_rejects_invalid_kind(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        conv.kind = "invalid_kind"
        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(conv)

    def test_sub_agent_kind(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        parent = _make_conversation(id="conv_parent")
        child = _make_conversation(
            id="conv_child",
            kind="sub_agent",
            parent_conversation_id="conv_parent",
            root_conversation_id="conv_parent",
            title="summarizer",
        )
        with managed() as session:
            session.add(parent)
            session.add(child)

        with managed() as session:
            loaded = session.get(SqlConversation, "conv_child")
            assert loaded is not None
            assert loaded.kind == "sub_agent"
            assert loaded.parent_conversation_id == "conv_parent"
            assert loaded.root_conversation_id == "conv_parent"

    def test_cascade_delete_removes_children(self, db_uri: str) -> None:
        """Deleting a parent conversation cascades to child conversations."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        parent = _make_conversation(id="conv_parent2")
        child = _make_conversation(
            id="conv_child2",
            kind="sub_agent",
            parent_conversation_id="conv_parent2",
            root_conversation_id="conv_parent2",
            title="child",
        )
        with managed() as session:
            session.add(parent)
            session.add(child)

        with managed() as session:
            p = session.get(SqlConversation, "conv_parent2")
            assert p is not None
            session.delete(p)

        with managed() as session:
            assert session.get(SqlConversation, "conv_child2") is None


# ── SqlConversationItem ───────────────────────────────


class TestSqlConversationItem:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        item = _make_item()
        with managed() as session:
            session.add(conv)
            session.add(item)

        with managed() as session:
            loaded = session.get(SqlConversationItem, "msg_test1")
            assert loaded is not None
            assert loaded.conversation_id == "conv_test1"
            assert loaded.type == "message"
            assert loaded.position == 0
            assert loaded.status == "completed"
            assert loaded.created_by is None

    def test_unique_position_per_conversation(self, db_uri: str) -> None:
        """Two items in the same conversation cannot share the same position."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        item1 = _make_item(id="msg_1", position=0)
        item2 = _make_item(id="msg_2", position=0)

        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(conv)
                session.add(item1)
                session.add(item2)

    def test_cascade_delete_with_conversation(self, db_uri: str) -> None:
        """Deleting a conversation cascades to its items."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation(id="conv_del")
        item = _make_item(id="msg_del", conversation_id="conv_del")
        with managed() as session:
            session.add(conv)
            session.add(item)

        with managed() as session:
            c = session.get(SqlConversation, "conv_del")
            assert c is not None
            session.delete(c)

        with managed() as session:
            assert session.get(SqlConversationItem, "msg_del") is None

    def test_multiple_items_ordered_by_position(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        with managed() as session:
            session.add(conv)
            for i in range(5):
                session.add(_make_item(id=f"msg_{i}", position=i))

        with managed() as session:
            items = (
                session.query(SqlConversationItem)
                .filter_by(conversation_id="conv_test1")
                .order_by(SqlConversationItem.position)
                .all()
            )
            assert len(items) == 5
            assert [it.position for it in items] == [0, 1, 2, 3, 4]


# ── SqlConversationLabel ──────────────────────────────


class TestSqlConversationLabel:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        label = SqlConversationLabel(
            conversation_id="conv_test1",
            key="sensitivity",
            value="confidential",
            updated_at=_now(),
        )
        with managed() as session:
            session.add(conv)
            session.add(label)

        with managed() as session:
            loaded = (
                session.query(SqlConversationLabel)
                .filter_by(conversation_id="conv_test1", key="sensitivity")
                .one()
            )
            assert loaded.value == "confidential"

    def test_composite_pk_allows_different_keys(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        with managed() as session:
            session.add(conv)
            session.add(
                SqlConversationLabel(
                    conversation_id="conv_test1", key="k1", value="v1", updated_at=_now()
                )
            )
            session.add(
                SqlConversationLabel(
                    conversation_id="conv_test1", key="k2", value="v2", updated_at=_now()
                )
            )

        with managed() as session:
            labels = (
                session.query(SqlConversationLabel).filter_by(conversation_id="conv_test1").all()
            )
            assert len(labels) == 2


# ── SqlSessionPermission ─────────────────────────────


class TestSqlSessionPermission:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        with managed() as session:
            session.add(SqlUser(id="alice@example.com", is_admin=False))
            session.add(_make_conversation())

        perm = SqlSessionPermission(
            user_id="alice@example.com",
            conversation_id="conv_test1",
            level=2,
        )
        with managed() as session:
            session.add(perm)

        with managed() as session:
            loaded = session.get(SqlSessionPermission, ("alice@example.com", "conv_test1"))
            assert loaded is not None
            assert loaded.level == 2

    def test_check_constraint_rejects_invalid_level(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        with managed() as session:
            session.add(SqlUser(id="bob@example.com", is_admin=False))
            session.add(_make_conversation())

        perm = SqlSessionPermission(
            user_id="bob@example.com",
            conversation_id="conv_test1",
            level=99,
        )
        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(perm)


# ── SqlComment ────────────────────────────────────────


class TestSqlComment:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        comment = SqlComment(
            id="cmt_test1",
            conversation_id="conv_test1",
            path="src/App.tsx",
            start_index=10,
            end_index=20,
            body="Looks good!",
            status="draft",
            created_at=now,
            updated_at=now * 1_000_000,
            anchor_content="selected text",
            created_by="alice@example.com",
        )
        conv = _make_conversation()
        with managed() as session:
            session.add(conv)
            session.add(comment)

        with managed() as session:
            loaded = session.get(SqlComment, "cmt_test1")
            assert loaded is not None
            assert loaded.path == "src/App.tsx"
            assert loaded.body == "Looks good!"
            assert loaded.status == "draft"
            assert loaded.anchor_content == "selected text"
            assert loaded.created_by == "alice@example.com"

    def test_nullable_anchor_and_created_by(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        comment = SqlComment(
            id="cmt_test2",
            conversation_id="conv_test1",
            path="README.md",
            start_index=0,
            end_index=5,
            body="Legacy comment",
            status="addressed",
            created_at=now,
            updated_at=now * 1_000_000,
        )
        conv = _make_conversation()
        with managed() as session:
            session.add(conv)
            session.add(comment)

        with managed() as session:
            loaded = session.get(SqlComment, "cmt_test2")
            assert loaded is not None
            assert loaded.anchor_content is None
            assert loaded.created_by is None


# ── SqlPolicy ─────────────────────────────────────────


class TestSqlPolicy:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        policy = SqlPolicy(
            id="pol_test1",
            name="cost-guard",
            created_at=_now(),
            type="python",
            handler="omnigent.policies.cost_guard:handler",
            enabled=True,
        )
        with managed() as session:
            session.add(policy)

        with managed() as session:
            loaded = session.get(SqlPolicy, "pol_test1")
            assert loaded is not None
            assert loaded.name == "cost-guard"
            assert loaded.type == "python"
            assert loaded.enabled is True
            assert loaded.session_id is None

    def test_unique_constraint_session_name(self, db_uri: str) -> None:
        """Two policies in the same session cannot share the same name."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        p1 = SqlPolicy(
            id="pol_1",
            name="guard",
            session_id="conv_test1",
            created_at=_now(),
            type="python",
            handler="mod:fn",
        )
        p2 = SqlPolicy(
            id="pol_2",
            name="guard",
            session_id="conv_test1",
            created_at=_now(),
            type="python",
            handler="mod:fn2",
        )
        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(conv)
                session.add(p1)
                session.add(p2)


# ── SqlHost ───────────────────────────────────────────


class TestSqlHost:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        host = SqlHost(
            owner="corey@example.com",
            name="corey-laptop",
            host_id="host_abc123",
            status="online",
            created_at=now,
            updated_at=now,
        )
        with managed() as session:
            session.add(host)

        with managed() as session:
            loaded = session.get(SqlHost, ("corey@example.com", "corey-laptop"))
            assert loaded is not None
            assert loaded.host_id == "host_abc123"
            assert loaded.status == "online"
            assert loaded.token_hash is None
            assert loaded.sandbox_provider is None

    def test_check_constraint_rejects_invalid_status(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        host = SqlHost(
            owner="owner",
            name="host",
            host_id="host_bad",
            status="unknown",
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(host)

    def test_unique_host_id(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        h1 = SqlHost(
            owner="a@x.com",
            name="h1",
            host_id="host_dup",
            status="online",
            created_at=now,
            updated_at=now,
        )
        h2 = SqlHost(
            owner="b@x.com",
            name="h2",
            host_id="host_dup",
            status="offline",
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(h1)
                session.add(h2)


# ── SqlUserDailyCost ──────────────────────────────────


class TestSqlUserDailyCost:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        row = SqlUserDailyCost(
            user_id="alice@example.com",
            day_utc="2026-06-16",
            cost_usd=1.23,
            ask_approved_usd=0.0,
            updated_at=_now(),
        )
        with managed() as session:
            session.add(row)

        with managed() as session:
            loaded = session.get(SqlUserDailyCost, ("alice@example.com", "2026-06-16"))
            assert loaded is not None
            assert loaded.cost_usd == pytest.approx(1.23)
            assert loaded.ask_approved_usd == pytest.approx(0.0)

    def test_composite_pk_multiple_days(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        with managed() as session:
            session.add(
                SqlUserDailyCost(
                    user_id="u1",
                    day_utc="2026-06-15",
                    cost_usd=1.0,
                    ask_approved_usd=0.0,
                    updated_at=_now(),
                )
            )
            session.add(
                SqlUserDailyCost(
                    user_id="u1",
                    day_utc="2026-06-16",
                    cost_usd=2.0,
                    ask_approved_usd=0.0,
                    updated_at=_now(),
                )
            )

        with managed() as session:
            rows = session.query(SqlUserDailyCost).filter_by(user_id="u1").all()
            assert len(rows) == 2
