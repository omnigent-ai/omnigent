"""Tests for :class:`SqlAlchemyPolicyStore`.

Exercises the ``create``, ``get``, ``list_for_session``, ``update``,
and ``delete`` methods against a real SQLite database.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.policy_store.sqlalchemy_store import (
    SqlAlchemyPolicyStore,
)


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyPolicyStore:
    """A fresh :class:`SqlAlchemyPolicyStore` backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemyPolicyStore` instance.
    """
    return SqlAlchemyPolicyStore(db_uri)


@pytest.fixture()
def session_id(db_uri: str) -> str:
    """Create a real conversation row and return its ID.

    Required because ``policies.session_id`` has a FK to
    ``conversations.id`` — raw strings fail the FK check.

    :param db_uri: Per-test SQLite URI.
    :returns: A conversation ID, e.g. ``"conv_abc123"``.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    return conv_store.create_conversation().id


@pytest.fixture()
def other_session_id(db_uri: str) -> str:
    """Create a second conversation row for cross-session isolation tests.

    :param db_uri: Per-test SQLite URI.
    :returns: A conversation ID different from :func:`session_id`.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    return conv_store.create_conversation().id


# ── create_session_policy ───────────────────────────────────────────────────


def test_create_returns_policy_with_correct_fields(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``create_session_policy`` returns a Policy with all fields echoed back.

    Verifies that the entity round-trips through the ORM layer without
    loss — session_id, handler, and nullable prompt-policy fields all
    map correctly.
    """
    policy = store.create(
        policy_id="pol_test1",
        session_id=session_id,
        name="block_push",
        type="python",
        handler="github_mcp_policy.block_push",
    )

    assert policy.id == "pol_test1"
    assert policy.session_id == session_id
    assert policy.name == "block_push"
    assert policy.type == "python"
    assert policy.handler == "github_mcp_policy.block_push"
    assert policy.enabled is True
    assert policy.created_at > 0
    assert policy.updated_at is None


def test_create_url_type(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``create_session_policy`` with ``type="url"`` stores an HTTP endpoint handler."""
    policy = store.create(
        policy_id="pol_url1",
        session_id=session_id,
        name="external_eval",
        type="url",
        handler="https://example.com/policies/eval",
    )

    assert policy.type == "url"
    assert policy.handler == "https://example.com/policies/eval"


def test_create_duplicate_name_raises(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``create_session_policy`` with a duplicate ``(session_id, name)`` raises IntegrityError."""
    store.create(
        policy_id="pol_dup1",
        session_id=session_id,
        name="dup_policy",
        type="python",
        handler="mod.func",
    )
    with pytest.raises(IntegrityError):
        store.create(
            policy_id="pol_dup2",
            session_id=session_id,
            name="dup_policy",
            type="python",
            handler="mod.func2",
        )


def test_create_same_name_different_sessions(
    store: SqlAlchemyPolicyStore,
    session_id: str,
    other_session_id: str,
) -> None:
    """Two sessions may have policies with the same name."""
    p1 = store.create(
        policy_id="pol_s1",
        session_id=session_id,
        name="shared_name",
        type="python",
        handler="mod.func",
    )
    p2 = store.create(
        policy_id="pol_s2",
        session_id=other_session_id,
        name="shared_name",
        type="python",
        handler="mod.func",
    )

    assert p1.id != p2.id
    assert p1.name == p2.name == "shared_name"


# ── get_session_policy ──────────────────────────────────────────────────────


def test_get_returns_policy(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``get_session_policy`` returns the policy when it belongs to the session."""
    created = store.create(
        policy_id="pol_get1",
        session_id=session_id,
        name="get_policy",
        type="python",
        handler="mod.func",
    )
    fetched = store.get("pol_get1", session_id)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.name == "get_policy"


def test_get_returns_none_for_missing(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``get_session_policy`` returns ``None`` when the policy does not exist."""
    assert store.get("pol_nonexistent", session_id) is None


def test_get_returns_none_for_wrong_session(
    store: SqlAlchemyPolicyStore,
    session_id: str,
    other_session_id: str,
) -> None:
    """``get_session_policy`` returns ``None`` for a different session.

    Prevents cross-session data leakage.
    """
    store.create(
        policy_id="pol_wrong",
        session_id=session_id,
        name="owned_policy",
        type="python",
        handler="mod.func",
    )
    assert store.get("pol_wrong", other_session_id) is None


# ── list_for_session ────────────────────────────────────────────────────────


def test_list_for_session_returns_policies_in_order(
    store: SqlAlchemyPolicyStore,
    session_id: str,
    other_session_id: str,
) -> None:
    """``list_for_session`` returns policies ordered by ``created_at ASC``.

    Also verifies session isolation — policies from other sessions
    must not appear.
    """
    store.create(
        policy_id="pol_list1",
        session_id=session_id,
        name="first",
        type="python",
        handler="mod.a",
    )
    store.create(
        policy_id="pol_list2",
        session_id=session_id,
        name="second",
        type="url",
        handler="https://example.com",
    )
    # Different session — should not appear.
    store.create(
        policy_id="pol_other",
        session_id=other_session_id,
        name="other",
        type="python",
        handler="mod.b",
    )

    policies = store.list_for_session(session_id)

    assert len(policies) == 2
    assert policies[0].name == "first"
    assert policies[1].name == "second"


def test_list_for_session_empty(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``list_for_session`` returns an empty list for a session with no policies."""
    assert store.list_for_session(session_id) == []


# ── update_session_policy ───────────────────────────────────────────────────


def test_update_changes_name(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``update_session_policy`` with ``name=`` changes the name and bumps ``updated_at``."""
    store.create(
        policy_id="pol_upd1",
        session_id=session_id,
        name="old_name",
        type="python",
        handler="mod.func",
    )
    updated = store.update("pol_upd1", session_id, name="new_name")

    assert updated is not None
    assert updated.name == "new_name"
    assert updated.updated_at is not None
    assert updated.updated_at > 0


def test_update_changes_enabled(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``update_session_policy`` with ``enabled=False`` disables the policy."""
    store.create(
        policy_id="pol_upd2",
        session_id=session_id,
        name="toggle_policy",
        type="python",
        handler="mod.func",
    )
    updated = store.update("pol_upd2", session_id, enabled=False)

    assert updated is not None
    assert updated.enabled is False


def test_update_changes_handler(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``update_session_policy`` with ``handler=`` changes the handler path."""
    store.create(
        policy_id="pol_upd3",
        session_id=session_id,
        name="handler_policy",
        type="python",
        handler="mod.old_func",
    )
    updated = store.update("pol_upd3", session_id, handler="mod.new_func")

    assert updated is not None
    assert updated.handler == "mod.new_func"


def test_update_noop_does_not_bump_timestamp(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``update_session_policy`` with no changes does not bump ``updated_at``."""
    store.create(
        policy_id="pol_noop",
        session_id=session_id,
        name="noop_policy",
        type="python",
        handler="mod.func",
    )
    updated = store.update("pol_noop", session_id)

    assert updated is not None
    assert updated.updated_at is None


def test_update_returns_none_for_missing(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``update_session_policy`` returns ``None`` when the policy does not exist."""
    assert store.update("pol_missing", session_id, name="x") is None


def test_update_returns_none_for_wrong_session(
    store: SqlAlchemyPolicyStore,
    session_id: str,
    other_session_id: str,
) -> None:
    """``update_session_policy`` returns ``None`` for a different session."""
    store.create(
        policy_id="pol_xsess",
        session_id=session_id,
        name="xsess_policy",
        type="python",
        handler="mod.func",
    )
    assert store.update("pol_xsess", other_session_id, enabled=False) is None


# ── delete_session_policy ───────────────────────────────────────────────────


def test_delete_removes_policy(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``delete_session_policy`` removes the policy and returns ``True``."""
    store.create(
        policy_id="pol_del1",
        session_id=session_id,
        name="to_delete",
        type="python",
        handler="mod.func",
    )
    assert store.delete("pol_del1", session_id) is True
    assert store.get("pol_del1", session_id) is None


def test_delete_idempotent(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``delete_session_policy`` on a missing policy returns ``False``."""
    assert store.delete("pol_missing", session_id) is False


def test_delete_wrong_session(
    store: SqlAlchemyPolicyStore,
    session_id: str,
    other_session_id: str,
) -> None:
    """``delete_session_policy`` returns ``False`` for a different session."""
    store.create(
        policy_id="pol_del_x",
        session_id=session_id,
        name="xdel_policy",
        type="python",
        handler="mod.func",
    )
    assert store.delete("pol_del_x", other_session_id) is False
    assert store.get("pol_del_x", session_id) is not None


# ── Default (server-wide) policy methods ──────────────────────────────────────


def test_create_default_returns_policy(store: SqlAlchemyPolicyStore) -> None:
    """create_default inserts a server-wide policy with session_id=None."""
    policy = store.create_default(
        policy_id="dpol_1",
        name="default_block",
        type="python",
        handler="mod.default_handler",
    )
    assert policy.id == "dpol_1"
    assert policy.session_id is None
    assert policy.name == "default_block"
    assert policy.type == "python"
    assert policy.handler == "mod.default_handler"
    assert policy.enabled is True
    assert policy.created_at > 0
    assert policy.updated_at is None


def test_create_default_with_factory_params(store: SqlAlchemyPolicyStore) -> None:
    """create_default stores factory_params as JSON."""
    policy = store.create_default(
        policy_id="dpol_fp",
        name="parameterized",
        type="python",
        handler="mod.func",
        factory_params={"threshold": 0.5, "mode": "strict"},
    )
    assert policy.factory_params == {"threshold": 0.5, "mode": "strict"}


def test_create_default_with_created_by(store: SqlAlchemyPolicyStore) -> None:
    """create_default stores the created_by field."""
    policy = store.create_default(
        policy_id="dpol_cb",
        name="audited",
        type="python",
        handler="mod.func",
        created_by="admin@example.com",
    )
    assert policy.created_by == "admin@example.com"


def test_create_default_duplicate_name_raises(store: SqlAlchemyPolicyStore) -> None:
    """create_default with a duplicate name raises IntegrityError."""
    store.create_default(
        policy_id="dpol_dup1",
        name="unique_default",
        type="python",
        handler="mod.func",
    )
    with pytest.raises(IntegrityError):
        store.create_default(
            policy_id="dpol_dup2",
            name="unique_default",
            type="python",
            handler="mod.func2",
        )


def test_create_default_same_name_as_session_policy_ok(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """A default policy may share a name with a session-scoped policy."""
    store.create(
        policy_id="pol_session",
        session_id=session_id,
        name="shared_name",
        type="python",
        handler="mod.func",
    )
    default = store.create_default(
        policy_id="dpol_shared",
        name="shared_name",
        type="python",
        handler="mod.default_func",
    )
    assert default.session_id is None


def test_get_default_returns_policy(store: SqlAlchemyPolicyStore) -> None:
    """get_default fetches a default policy by ID."""
    store.create_default(
        policy_id="dpol_get",
        name="fetchable",
        type="python",
        handler="mod.func",
    )
    fetched = store.get_default("dpol_get")
    assert fetched is not None
    assert fetched.id == "dpol_get"
    assert fetched.name == "fetchable"


def test_get_default_returns_none_for_missing(store: SqlAlchemyPolicyStore) -> None:
    """get_default returns None when policy does not exist."""
    assert store.get_default("dpol_missing") is None


def test_get_default_returns_none_for_session_policy(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """get_default returns None for a session-scoped policy."""
    store.create(
        policy_id="pol_sess_only",
        session_id=session_id,
        name="session_only",
        type="python",
        handler="mod.func",
    )
    assert store.get_default("pol_sess_only") is None


def test_list_defaults_returns_all_in_order(store: SqlAlchemyPolicyStore) -> None:
    """list_defaults returns all default policies ordered by created_at ASC."""
    store.create_default(policy_id="dpol_l1", name="first", type="python", handler="mod.a")
    store.create_default(policy_id="dpol_l2", name="second", type="python", handler="mod.b")
    defaults = store.list_defaults()
    assert len(defaults) == 2
    assert defaults[0].name == "first"
    assert defaults[1].name == "second"


def test_list_defaults_excludes_session_policies(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """list_defaults does not return session-scoped policies."""
    store.create(
        policy_id="pol_sess",
        session_id=session_id,
        name="session_pol",
        type="python",
        handler="mod.func",
    )
    store.create_default(
        policy_id="dpol_only",
        name="default_only",
        type="python",
        handler="mod.func",
    )
    defaults = store.list_defaults()
    assert len(defaults) == 1
    assert defaults[0].name == "default_only"


def test_list_defaults_empty(store: SqlAlchemyPolicyStore) -> None:
    """list_defaults returns empty list when no default policies exist."""
    assert store.list_defaults() == []


def test_update_default_changes_name(store: SqlAlchemyPolicyStore) -> None:
    """update_default with name= changes the name and bumps updated_at."""
    store.create_default(
        policy_id="dpol_upd1",
        name="old_name",
        type="python",
        handler="mod.func",
    )
    updated = store.update_default("dpol_upd1", name="new_name")
    assert updated is not None
    assert updated.name == "new_name"
    assert updated.updated_at is not None


def test_update_default_changes_handler(store: SqlAlchemyPolicyStore) -> None:
    """update_default with handler= changes the handler."""
    store.create_default(
        policy_id="dpol_upd2",
        name="handler_pol",
        type="python",
        handler="mod.old_func",
    )
    updated = store.update_default("dpol_upd2", handler="mod.new_func")
    assert updated is not None
    assert updated.handler == "mod.new_func"


def test_update_default_changes_enabled(store: SqlAlchemyPolicyStore) -> None:
    """update_default with enabled=False disables the policy."""
    store.create_default(
        policy_id="dpol_upd3",
        name="toggle_default",
        type="python",
        handler="mod.func",
    )
    updated = store.update_default("dpol_upd3", enabled=False)
    assert updated is not None
    assert updated.enabled is False


def test_update_default_noop_does_not_bump_timestamp(store: SqlAlchemyPolicyStore) -> None:
    """update_default with no changes does not bump updated_at."""
    store.create_default(
        policy_id="dpol_noop",
        name="noop_pol",
        type="python",
        handler="mod.func",
    )
    updated = store.update_default("dpol_noop")
    assert updated is not None
    assert updated.updated_at is None


def test_update_default_returns_none_for_missing(store: SqlAlchemyPolicyStore) -> None:
    """update_default returns None when policy does not exist."""
    assert store.update_default("dpol_missing", name="x") is None


def test_update_default_returns_none_for_session_policy(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """update_default returns None for a session-scoped policy."""
    store.create(
        policy_id="pol_not_default",
        session_id=session_id,
        name="not_default",
        type="python",
        handler="mod.func",
    )
    assert store.update_default("pol_not_default", name="new") is None


def test_update_default_duplicate_name_raises(store: SqlAlchemyPolicyStore) -> None:
    """update_default rejects a name that collides with another default."""
    store.create_default(
        policy_id="dpol_ren1",
        name="name_a",
        type="python",
        handler="mod.func",
    )
    store.create_default(
        policy_id="dpol_ren2",
        name="name_b",
        type="python",
        handler="mod.func",
    )
    with pytest.raises(IntegrityError):
        store.update_default("dpol_ren2", name="name_a")


def test_delete_default_removes_policy(store: SqlAlchemyPolicyStore) -> None:
    """delete_default removes the policy and returns True."""
    store.create_default(
        policy_id="dpol_del1",
        name="to_delete",
        type="python",
        handler="mod.func",
    )
    assert store.delete_default("dpol_del1") is True
    assert store.get_default("dpol_del1") is None


def test_delete_default_idempotent(store: SqlAlchemyPolicyStore) -> None:
    """delete_default on a missing policy returns False."""
    assert store.delete_default("dpol_missing") is False


def test_delete_default_rejects_session_policy(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """delete_default returns False for a session-scoped policy."""
    store.create(
        policy_id="pol_no_del",
        session_id=session_id,
        name="cant_delete_as_default",
        type="python",
        handler="mod.func",
    )
    assert store.delete_default("pol_no_del") is False
