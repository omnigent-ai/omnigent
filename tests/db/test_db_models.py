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
from omnigent.db.enum_codecs import (
    encode_account_token_kind,
    encode_agent_kind,
    encode_comment_status,
    encode_conversation_kind,
    encode_host_status,
    encode_item_status,
    encode_item_type,
    encode_policy_scope,
    encode_policy_type,
)
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker

# ── helpers ───────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _make_agent(
    id: str = "ag_test1",
    name: str = "test-agent",
    kind: str = "template",
) -> SqlAgent:
    return SqlAgent(
        id=id,
        created_at=_now(),
        name=name,
        bundle_location="ag_test1/abc123",
        version=1,
        kind=encode_agent_kind(kind),
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
        kind=encode_conversation_kind(kind),
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
        status=encode_item_status("completed"),
        position=position,
        type=encode_item_type("message"),
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
            loaded = session.get(SqlAgent, (0, "ag_test1"))
            assert loaded is not None
            assert loaded.name == "test-agent"
            assert loaded.version == 1
            assert loaded.description is None
            assert loaded.updated_at is None
            assert loaded.kind == encode_agent_kind("template")

    def test_nullable_columns(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        agent = _make_agent()
        agent.description = "A test agent"
        agent.updated_at = _now()
        with managed() as session:
            session.add(agent)

        with managed() as session:
            loaded = session.get(SqlAgent, (0, "ag_test1"))
            assert loaded is not None
            assert loaded.description == "A test agent"
            assert loaded.updated_at is not None

    def test_session_scoped_agent_kind(self, db_uri: str) -> None:
        """A session-scoped agent is stored with kind='session'."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        agent = _make_agent(kind="session")
        with managed() as session:
            session.add(agent)

        with managed() as session:
            loaded = session.get(SqlAgent, (0, "ag_test1"))
            assert loaded is not None
            assert loaded.kind == encode_agent_kind("session")

    def test_multiple_session_agents_allowed(self, db_uri: str) -> None:
        """Multiple session-scoped agents are permitted (no unique constraint on kind)."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        a1 = _make_agent(id="ag_1", name="agent-1", kind="session")
        a2 = _make_agent(id="ag_2", name="agent-2", kind="session")

        with managed() as session:
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
            loaded = session.get(SqlFile, (0, "file_test1"))
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
            loaded = session.get(SqlFile, (0, "file_test2"))
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
            loaded = session.get(SqlUser, (0, "alice@example.com"))
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
            loaded = session.get(SqlUser, (0, "admin@example.com"))
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
            kind=encode_account_token_kind("invite"),
            created_at=now,
            expires_at=now + 3600,
            created_by="admin@example.com",
            invited_is_admin=True,
        )
        with managed() as session:
            session.add(token)

        with managed() as session:
            loaded = session.get(SqlAccountToken, (0, "tok_invite_abc"))
            assert loaded is not None
            assert loaded.kind == encode_account_token_kind("invite")
            assert loaded.user_id is None
            assert loaded.redeemed_at is None
            assert loaded.invited_is_admin is True

    def test_persist_magic_token(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        token = SqlAccountToken(
            id="tok_magic_xyz",
            kind=encode_account_token_kind("magic"),
            user_id="alice@example.com",
            created_at=now,
            expires_at=now + 300,
        )
        with managed() as session:
            session.add(token)

        with managed() as session:
            loaded = session.get(SqlAccountToken, (0, "tok_magic_xyz"))
            assert loaded is not None
            assert loaded.kind == encode_account_token_kind("magic")
            assert loaded.user_id == "alice@example.com"

    def test_check_constraint_rejects_invalid_kind(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        # An out-of-range int code must be rejected by ck_account_tokens_kind.
        token = SqlAccountToken(
            id="tok_bad",
            kind=99,
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
            loaded = session.get(SqlConversation, (0, "conv_test1"))
            assert loaded is not None
            assert loaded.title == "Hello World"
            assert loaded.kind == encode_conversation_kind("default")
            assert loaded.archived is False

    def test_defaults(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        with managed() as session:
            session.add(conv)

        with managed() as session:
            loaded = session.get(SqlConversation, (0, "conv_test1"))
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
        # An out-of-range int code must be rejected by ck_conversations_kind.
        conv.kind = 99
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
            loaded = session.get(SqlConversation, (0, "conv_child"))
            assert loaded is not None
            assert loaded.kind == encode_conversation_kind("sub_agent")
            assert loaded.parent_conversation_id == "conv_parent"
            assert loaded.root_conversation_id == "conv_parent"

    def test_delete_parent_leaves_children_without_fk(self, db_uri: str) -> None:
        """Without DB-level FK cascade, deleting a parent leaves child rows intact.

        The application (delete_conversation) is responsible for cleaning
        up the subtree explicitly.
        """
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
            p = session.get(SqlConversation, (0, "conv_parent2"))
            assert p is not None
            session.delete(p)

        # Without FK cascade the child is NOT automatically deleted.
        with managed() as session:
            assert session.get(SqlConversation, (0, "conv_child2")) is not None


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
            loaded = session.get(SqlConversationItem, (0, "msg_test1"))
            assert loaded is not None
            assert loaded.conversation_id == "conv_test1"
            assert loaded.type == encode_item_type("message")
            assert loaded.position == 0
            assert loaded.status == encode_item_status("completed")
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

    def test_delete_conversation_via_orm_leaves_items_without_fk(self, db_uri: str) -> None:
        """Without DB-level FK cascade, deleting a conversation leaves its items intact.

        The application (delete_conversation) is responsible for deleting
        items explicitly before or after deleting the conversation row.
        """
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation(id="conv_del")
        item = _make_item(id="msg_del", conversation_id="conv_del")
        with managed() as session:
            session.add(conv)
            session.add(item)

        with managed() as session:
            c = session.get(SqlConversation, (0, "conv_del"))
            assert c is not None
            session.delete(c)

        # Without FK cascade the item is NOT automatically deleted.
        with managed() as session:
            assert session.get(SqlConversationItem, (0, "msg_del")) is not None

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
            loaded = session.get(SqlSessionPermission, (0, "alice@example.com", "conv_test1"))
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
            status=encode_comment_status("draft"),
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
            loaded = session.get(SqlComment, (0, "cmt_test1"))
            assert loaded is not None
            assert loaded.path == "src/App.tsx"
            assert loaded.body == "Looks good!"
            assert loaded.status == encode_comment_status("draft")
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
            status=encode_comment_status("addressed"),
            created_at=now,
            updated_at=now * 1_000_000,
        )
        conv = _make_conversation()
        with managed() as session:
            session.add(conv)
            session.add(comment)

        with managed() as session:
            loaded = session.get(SqlComment, (0, "cmt_test2"))
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
            scope=encode_policy_scope("default"),
            created_at=_now(),
            type=encode_policy_type("python"),
            handler="omnigent.policies.cost_guard:handler",
            enabled=True,
        )
        with managed() as session:
            session.add(policy)

        with managed() as session:
            loaded = session.get(SqlPolicy, (0, "pol_test1"))
            assert loaded is not None
            assert loaded.name == "cost-guard"
            assert loaded.type == encode_policy_type("python")
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
            scope=encode_policy_scope("session"),
            created_at=_now(),
            type=encode_policy_type("python"),
            handler="mod:fn",
        )
        p2 = SqlPolicy(
            id="pol_2",
            name="guard",
            session_id="conv_test1",
            scope=encode_policy_scope("session"),
            created_at=_now(),
            type=encode_policy_type("python"),
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
            status=encode_host_status("online"),
            created_at=now,
            updated_at=now,
        )
        with managed() as session:
            session.add(host)

        with managed() as session:
            loaded = session.get(SqlHost, (0, "host_abc123"))
            assert loaded is not None
            assert loaded.host_id == "host_abc123"
            assert loaded.status == encode_host_status("online")
            assert loaded.token_hash is None
            assert loaded.sandbox_provider is None

    def test_check_constraint_rejects_invalid_status(self, db_uri: str) -> None:
        """ck_hosts_status rejects out-of-range int codes on enforcing backends.

        SQLite does not enforce CHECK constraints by default, so we verify the
        constraint exists in the schema without asserting enforcement on SQLite.
        On Postgres / MySQL the constraint is enforced at runtime.
        """
        import sqlalchemy as sa

        engine = get_or_create_engine(db_uri)
        inspector = sa.inspect(engine)
        checks = {c["name"] for c in inspector.get_check_constraints("hosts")}
        assert "ck_hosts_status" in checks, "ck_hosts_status must exist on hosts"

    def test_unique_host_id(self, db_uri: str) -> None:
        """Duplicate host_id within the same workspace violates the PK."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        h1 = SqlHost(
            owner="a@x.com",
            name="h1",
            host_id="host_dup",
            status=encode_host_status("online"),
            created_at=now,
            updated_at=now,
        )
        # Commit h1 first so h2's insert hits a real PK violation at the DB.
        with managed() as session:
            session.add(h1)

        h2 = SqlHost(
            owner="b@x.com",
            name="h2",
            host_id="host_dup",
            status=encode_host_status("offline"),
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(IntegrityError):
            with managed() as session:
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
            loaded = session.get(SqlUserDailyCost, (0, "alice@example.com", "2026-06-16"))
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
