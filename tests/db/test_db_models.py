"""Tests for SQLAlchemy ORM models (omnigent/db/db_models.py).

Verifies that each ORM model can be instantiated, persisted, read back,
and that relationships, defaults, nullable columns, and constraints
behave as expected.
"""

from __future__ import annotations

import hashlib
import time

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from omnigent.db.db_models import (
    SqlAccountToken,
    SqlAgent,
    SqlComment,
    SqlConversation,
    SqlConversationItem,
    SqlConversationLabel,
    SqlConversationMetadata,
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
    id: str = "0ecf75a6ff1ff86bcc1902eb0951ef45",
    name: str = "test-agent",
    kind: str = "template",
) -> SqlAgent:
    return SqlAgent(
        id=id,
        created_at=_now(),
        name=name,
        bundle_location="0ecf75a6ff1ff86bcc1902eb0951ef45/abc123",
        version=1,
        kind=encode_agent_kind(kind),
    )


def _make_conversation(
    id: str = "a9930027fd3e2e979e65844f7af7bf88",
    parent_conversation_id: str | None = None,
    root_conversation_id: str | None = None,
    title: str | None = None,
    archived: bool = False,
    agent_id: str | None = None,
    session_overrides: str | None = None,
) -> SqlConversation:
    return SqlConversation(
        id=id,
        created_at=_now(),
        updated_at=_now(),
        parent_conversation_id=parent_conversation_id,
        root_conversation_id=root_conversation_id or id,
        title=title,
        archived=archived,
        agent_id=agent_id,
        session_overrides=session_overrides,
    )


def _make_metadata(
    id: str = "a9930027fd3e2e979e65844f7af7bf88",
    kind: str = "default",
) -> SqlConversationMetadata:
    return SqlConversationMetadata(
        id=id,
        kind=encode_conversation_kind(kind),
    )


def _make_item(
    id: str = "a47da81d0587d7c42e53978d629c5ab8",
    conversation_id: str = "a9930027fd3e2e979e65844f7af7bf88",
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
            loaded = session.get(SqlAgent, (0, "0ecf75a6ff1ff86bcc1902eb0951ef45"))
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
            loaded = session.get(SqlAgent, (0, "0ecf75a6ff1ff86bcc1902eb0951ef45"))
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
            loaded = session.get(SqlAgent, (0, "0ecf75a6ff1ff86bcc1902eb0951ef45"))
            assert loaded is not None
            assert loaded.kind == encode_agent_kind("session")

    def test_multiple_session_agents_allowed(self, db_uri: str) -> None:
        """Multiple session-scoped agents are permitted (no unique constraint on kind)."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        a1 = _make_agent(id="880b5afda28ad55ff74cbeb9b5fc67fb", name="agent-1", kind="session")
        a2 = _make_agent(id="0fa804039e209a10554da55135751438", name="agent-2", kind="session")

        with managed() as session:
            session.add(a1)
            session.add(a2)


# ── SqlFile ───────────────────────────────────────────


class TestSqlFile:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        f = SqlFile(
            id="c9b7bd37959cc093d2b9e9ebf4d9b35b",
            created_at=_now(),
            filename="report.pdf",
            bytes=12345,
            content_type="application/pdf",
        )
        with managed() as session:
            session.add(f)

        with managed() as session:
            loaded = session.get(SqlFile, (0, "c9b7bd37959cc093d2b9e9ebf4d9b35b"))
            assert loaded is not None
            assert loaded.filename == "report.pdf"
            assert loaded.bytes == 12345
            assert loaded.content_type == "application/pdf"

    def test_nullable_content_type(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        f = SqlFile(
            id="aa87c404604d8dda9990e960edcd06b4",
            created_at=_now(),
            filename="data.bin",
            bytes=100,
        )
        with managed() as session:
            session.add(f)

        with managed() as session:
            loaded = session.get(SqlFile, (0, "aa87c404604d8dda9990e960edcd06b4"))
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
        with pytest.raises((IntegrityError, OperationalError)):
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
            loaded = session.get(SqlConversation, (0, "a9930027fd3e2e979e65844f7af7bf88"))
            assert loaded is not None
            assert loaded.title == "Hello World"
            # kind lives on SqlConversationMetadata; the agent binding
            # (agent_id) and per-session override blob live on the row itself.
            assert loaded.root_conversation_id == "a9930027fd3e2e979e65844f7af7bf88"
            assert loaded.next_position == 0

    def test_defaults(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        with managed() as session:
            session.add(conv)

        with managed() as session:
            loaded = session.get(SqlConversation, (0, "a9930027fd3e2e979e65844f7af7bf88"))
            assert loaded is not None
            # Agent binding + overrides default to NULL on a bare conversation.
            assert loaded.agent_id is None
            assert loaded.session_overrides is None

    def test_metadata_kind_and_archived(self, db_uri: str) -> None:
        """kind lives on SqlConversationMetadata; archived on SqlConversation."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        meta = _make_metadata(kind="default")
        with managed() as session:
            session.add(conv)
            session.add(meta)

        with managed() as session:
            loaded_meta = session.get(
                SqlConversationMetadata, (0, "a9930027fd3e2e979e65844f7af7bf88")
            )
            assert loaded_meta is not None
            assert loaded_meta.kind == encode_conversation_kind("default")
            loaded_conv = session.get(SqlConversation, (0, "a9930027fd3e2e979e65844f7af7bf88"))
            assert loaded_conv is not None
            assert loaded_conv.archived is False

    def test_metadata_check_constraint_rejects_invalid_kind(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        meta = _make_metadata()
        meta.kind = 99  # out-of-range — rejected by ck_conversation_metadata_kind
        with pytest.raises((IntegrityError, OperationalError)):
            with managed() as session:
                session.add(meta)

    def test_sub_agent_kind(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        parent = _make_conversation(id="ead6d59a6b650d19dbdf61ec32426f4e")
        parent_meta = _make_metadata(id="ead6d59a6b650d19dbdf61ec32426f4e", kind="default")
        child = _make_conversation(
            id="ff5cac23d0beb79fad914046049f32ff",
            parent_conversation_id="ead6d59a6b650d19dbdf61ec32426f4e",
            root_conversation_id="ead6d59a6b650d19dbdf61ec32426f4e",
            title="summarizer",
        )
        child_meta = _make_metadata(id="ff5cac23d0beb79fad914046049f32ff", kind="sub_agent")
        with managed() as session:
            session.add(parent)
            session.add(parent_meta)
            session.add(child)
            session.add(child_meta)

        with managed() as session:
            loaded = session.get(SqlConversation, (0, "ff5cac23d0beb79fad914046049f32ff"))
            assert loaded is not None
            assert loaded.parent_conversation_id == "ead6d59a6b650d19dbdf61ec32426f4e"
            assert loaded.root_conversation_id == "ead6d59a6b650d19dbdf61ec32426f4e"
            loaded_meta = session.get(
                SqlConversationMetadata, (0, "ff5cac23d0beb79fad914046049f32ff")
            )
            assert loaded_meta is not None
            assert loaded_meta.kind == encode_conversation_kind("sub_agent")

    def test_delete_parent_leaves_children_without_fk(self, db_uri: str) -> None:
        """Without DB-level FK cascade, deleting a parent leaves child rows intact.

        The application (delete_conversation) is responsible for cleaning
        up the subtree explicitly.
        """
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        parent = _make_conversation(id="14e0b0bdc49cbf48b2f92ba938beadb9")
        child = _make_conversation(
            id="6dbb0cf5f866fe63f24768195cc85646",
            parent_conversation_id="14e0b0bdc49cbf48b2f92ba938beadb9",
            root_conversation_id="14e0b0bdc49cbf48b2f92ba938beadb9",
            title="child",
        )
        with managed() as session:
            session.add(parent)
            session.add(child)

        with managed() as session:
            p = session.get(SqlConversation, (0, "14e0b0bdc49cbf48b2f92ba938beadb9"))
            assert p is not None
            session.delete(p)

        # Without FK cascade the child is NOT automatically deleted.
        with managed() as session:
            assert (
                session.get(SqlConversation, (0, "6dbb0cf5f866fe63f24768195cc85646")) is not None
            )


# ── SqlConversationItem ───────────────────────────────


class TestSqlConversationItem:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        item = _make_item()
        item_created_at = item.created_at
        with managed() as session:
            session.add(conv)
            session.add(item)

        with managed() as session:
            loaded = session.get(
                SqlConversationItem,
                (
                    0,
                    "a9930027fd3e2e979e65844f7af7bf88",
                    "a47da81d0587d7c42e53978d629c5ab8",
                    item_created_at,
                ),
            )
            assert loaded is not None
            assert loaded.conversation_id == "a9930027fd3e2e979e65844f7af7bf88"
            assert loaded.type == encode_item_type("message")
            assert loaded.position == 0
            assert loaded.status == encode_item_status("completed")
            assert loaded.created_by is None

    def test_position_not_unique_at_db_level(self, db_uri: str) -> None:
        """Two items in one conversation may share a position at the DB level.

        The position index is plain (non-unique); strict position uniqueness is
        owned by the ``next_position`` allocator under ``_lock_conversation``, not
        the index (see the store's concurrent-append test). Distinct ``id`` keeps
        the PK unique, so both rows persist.
        """
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        item1 = _make_item(id="9980c8a9248139f14f4165e5d53088aa", position=0)
        item2 = _make_item(id="0fd4e86b2daa009cd9929641dbd7dab6", position=0)

        # No IntegrityError: the DB does not enforce position uniqueness anymore.
        with managed() as session:
            session.add(conv)
            session.add(item1)
            session.add(item2)

        with managed() as session:
            count = session.query(SqlConversationItem).filter_by(conversation_id=conv.id).count()
        assert count == 2, f"both position-0 items should persist; found {count}"

    def test_delete_conversation_via_orm_leaves_items_without_fk(self, db_uri: str) -> None:
        """Without DB-level FK cascade, deleting a conversation leaves its items intact.

        The application (delete_conversation) is responsible for deleting
        items explicitly before or after deleting the conversation row.
        """
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation(id="553a265445caf1cdb034abe0b449485d")
        item = _make_item(
            id="79770462a714e18289f416144611383e",
            conversation_id="553a265445caf1cdb034abe0b449485d",
        )
        item_created_at = item.created_at
        with managed() as session:
            session.add(conv)
            session.add(item)

        with managed() as session:
            c = session.get(SqlConversation, (0, "553a265445caf1cdb034abe0b449485d"))
            assert c is not None
            session.delete(c)

        # Without FK cascade the item is NOT automatically deleted.
        with managed() as session:
            assert (
                session.get(
                    SqlConversationItem,
                    (
                        0,
                        "553a265445caf1cdb034abe0b449485d",
                        "79770462a714e18289f416144611383e",
                        item_created_at,
                    ),
                )
                is not None
            )

    def test_multiple_items_ordered_by_position(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        conv = _make_conversation()
        with managed() as session:
            session.add(conv)
            for i in range(5):
                session.add(_make_item(id=f"{i:032x}", position=i))

        with managed() as session:
            items = (
                session.query(SqlConversationItem)
                .filter_by(conversation_id="a9930027fd3e2e979e65844f7af7bf88")
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
            conversation_id="a9930027fd3e2e979e65844f7af7bf88",
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
                .filter_by(conversation_id="a9930027fd3e2e979e65844f7af7bf88", key="sensitivity")
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
                    conversation_id="a9930027fd3e2e979e65844f7af7bf88",
                    key="k1",
                    value="v1",
                    updated_at=_now(),
                )
            )
            session.add(
                SqlConversationLabel(
                    conversation_id="a9930027fd3e2e979e65844f7af7bf88",
                    key="k2",
                    value="v2",
                    updated_at=_now(),
                )
            )

        with managed() as session:
            labels = (
                session.query(SqlConversationLabel)
                .filter_by(conversation_id="a9930027fd3e2e979e65844f7af7bf88")
                .all()
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
            conversation_id="a9930027fd3e2e979e65844f7af7bf88",
            level=2,
        )
        with managed() as session:
            session.add(perm)

        with managed() as session:
            loaded = session.get(
                SqlSessionPermission, (0, "alice@example.com", "a9930027fd3e2e979e65844f7af7bf88")
            )
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
            conversation_id="a9930027fd3e2e979e65844f7af7bf88",
            level=99,
        )
        with pytest.raises((IntegrityError, OperationalError)):
            with managed() as session:
                session.add(perm)


# ── SqlComment ────────────────────────────────────────


class TestSqlComment:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        comment = SqlComment(
            id="cccccccccccccccccccccccccccccc01",
            conversation_id="a9930027fd3e2e979e65844f7af7bf88",
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
            loaded = session.get(
                SqlComment,
                (0, "a9930027fd3e2e979e65844f7af7bf88", "cccccccccccccccccccccccccccccc01"),
            )
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
            id="cccccccccccccccccccccccccccccc02",
            conversation_id="a9930027fd3e2e979e65844f7af7bf88",
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
            loaded = session.get(
                SqlComment,
                (0, "a9930027fd3e2e979e65844f7af7bf88", "cccccccccccccccccccccccccccccc02"),
            )
            assert loaded is not None
            assert loaded.anchor_content is None
            assert loaded.created_by is None


# ── SqlPolicy ─────────────────────────────────────────


class TestSqlPolicy:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        policy = SqlPolicy(
            id="21cbc70e914e5189cd9a57cf7d91eaba",
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
            loaded = session.get(SqlPolicy, (0, "21cbc70e914e5189cd9a57cf7d91eaba"))
            assert loaded is not None
            assert loaded.name == "cost-guard"
            # The column default stamps sha256(name) on INSERT.
            assert loaded.name_cksum == hashlib.sha256(b"cost-guard").digest()
            assert loaded.type == encode_policy_type("python")
            assert loaded.enabled is True
            assert loaded.session_id is None

    def test_session_name_uniqueness_not_db_enforced(self, db_uri: str) -> None:
        """Session-name uniqueness lives in the store, not a DB constraint.

        The ``(session_id, name_cksum)`` unique key was dropped
        (migration d4c1b9e6f3a2), so the raw table accepts two session
        policies that share a name. ``SqlAlchemyPolicyStore.create`` is
        the guard (see ``tests/stores/test_session_policy_store.py``).
        """
        import sqlalchemy as sa

        engine = get_or_create_engine(db_uri)

        uniques = {u["name"] for u in sa.inspect(engine).get_unique_constraints("policies")}
        assert "uq_policies_session_id_name_cksum" not in uniques

        managed = make_managed_session_maker(engine)
        p1 = SqlPolicy(
            id="12a6858438cb1aa1b9e00dc79bb04dd9",
            name="guard",
            session_id="a9930027fd3e2e979e65844f7af7bf88",
            scope=encode_policy_scope("session"),
            created_at=_now(),
            type=encode_policy_type("python"),
            handler="mod:fn",
        )
        p2 = SqlPolicy(
            id="532212bcd2f88d1ad1a0072b5a78a740",
            name="guard",
            session_id="a9930027fd3e2e979e65844f7af7bf88",
            scope=encode_policy_scope("session"),
            created_at=_now(),
            type=encode_policy_type("python"),
            handler="mod:fn2",
        )
        # No IntegrityError: the DB permits the duplicate now.
        with managed() as session:
            session.add(p1)
            session.add(p2)

        with managed() as session:
            rows = (
                session.execute(sa.select(SqlPolicy).where(SqlPolicy.name == "guard"))
                .scalars()
                .all()
            )
            assert len(rows) == 2


# ── SqlHost ───────────────────────────────────────────


class TestSqlHost:
    def test_persist_and_read(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        now = _now()
        host = SqlHost(
            user_id="corey@example.com",
            name="corey-laptop",
            host_id="4f64b6ee625f4e8259185c35c6e63f3d",
            status=encode_host_status("online"),
            created_at=now,
            updated_at=now,
        )
        with managed() as session:
            session.add(host)

        with managed() as session:
            loaded = session.get(SqlHost, (0, "4f64b6ee625f4e8259185c35c6e63f3d"))
            assert loaded is not None
            assert loaded.host_id == "4f64b6ee625f4e8259185c35c6e63f3d"
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
            user_id="a@x.com",
            name="h1",
            host_id="2690ed5ead1b05791d642d85e6847680",
            status=encode_host_status("online"),
            created_at=now,
            updated_at=now,
        )
        # Commit h1 first so h2's insert hits a real PK violation at the DB.
        with managed() as session:
            session.add(h1)

        h2 = SqlHost(
            user_id="b@x.com",
            name="h2",
            host_id="2690ed5ead1b05791d642d85e6847680",
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
