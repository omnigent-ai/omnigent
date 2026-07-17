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
    :returns: A conversation ID, e.g. ``"d1f9214d74c38b9f9a9db17ed8352dc4"``.
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
        policy_id="21cbc70e914e5189cd9a57cf7d91eaba",
        session_id=session_id,
        name="block_push",
        type="python",
        handler="github_mcp_policy.block_push",
    )

    assert policy.id == "21cbc70e914e5189cd9a57cf7d91eaba"
    assert policy.session_id == session_id
    assert policy.scope == "session"
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
        policy_id="850f7326576e58d827c93254ddd37859",
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
        policy_id="545045eca9c6eb6b4d328f2d4776127d",
        session_id=session_id,
        name="dup_policy",
        type="python",
        handler="mod.func",
    )
    with pytest.raises(IntegrityError):
        store.create(
            policy_id="568e203a2cd253403e2d849333f3a1a9",
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
        policy_id="6057393a9ceea2c3beb1260a0b4e7a28",
        session_id=session_id,
        name="shared_name",
        type="python",
        handler="mod.func",
    )
    p2 = store.create(
        policy_id="0813a850ccdb6f9c9ce85258f824cb4e",
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
        policy_id="d091f405442fb649d0ab930564bb4c4e",
        session_id=session_id,
        name="get_policy",
        type="python",
        handler="mod.func",
    )
    fetched = store.get("d091f405442fb649d0ab930564bb4c4e", session_id)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.name == "get_policy"


def test_get_returns_none_for_missing(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``get_session_policy`` returns ``None`` when the policy does not exist."""
    assert store.get("087a5ba1a5c50583fc5bd2e3f035d3df", session_id) is None


def test_get_returns_none_for_wrong_session(
    store: SqlAlchemyPolicyStore,
    session_id: str,
    other_session_id: str,
) -> None:
    """``get_session_policy`` returns ``None`` for a different session.

    Prevents cross-session data leakage.
    """
    store.create(
        policy_id="6965422b949aaa2efc377beba6215000",
        session_id=session_id,
        name="owned_policy",
        type="python",
        handler="mod.func",
    )
    assert store.get("6965422b949aaa2efc377beba6215000", other_session_id) is None


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
        policy_id="3af4d9de715e3e8b06b2f822970abd4a",
        session_id=session_id,
        name="first",
        type="python",
        handler="mod.a",
    )
    store.create(
        policy_id="3c253fc0ed81800d81cb7cbb6397534f",
        session_id=session_id,
        name="second",
        type="url",
        handler="https://example.com",
    )
    # Different session — should not appear.
    store.create(
        policy_id="a768c0b59d204dfbc4fdacbd5f7ee2c2",
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
        policy_id="bda355216e7cfc74b4a5e99e18a77765",
        session_id=session_id,
        name="old_name",
        type="python",
        handler="mod.func",
    )
    updated = store.update("bda355216e7cfc74b4a5e99e18a77765", session_id, name="new_name")

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
        policy_id="a47e5adf5d657541bfab9cf3ddc212fa",
        session_id=session_id,
        name="toggle_policy",
        type="python",
        handler="mod.func",
    )
    updated = store.update("a47e5adf5d657541bfab9cf3ddc212fa", session_id, enabled=False)

    assert updated is not None
    assert updated.enabled is False


def test_update_changes_handler(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``update_session_policy`` with ``handler=`` changes the handler path."""
    store.create(
        policy_id="9a054e5f00dd887f19433ce8e4bac50e",
        session_id=session_id,
        name="handler_policy",
        type="python",
        handler="mod.old_func",
    )
    updated = store.update("9a054e5f00dd887f19433ce8e4bac50e", session_id, handler="mod.new_func")

    assert updated is not None
    assert updated.handler == "mod.new_func"


def test_update_noop_does_not_bump_timestamp(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``update_session_policy`` with no changes does not bump ``updated_at``."""
    store.create(
        policy_id="3149668342eedf489ffc3186ea5bff28",
        session_id=session_id,
        name="noop_policy",
        type="python",
        handler="mod.func",
    )
    updated = store.update("3149668342eedf489ffc3186ea5bff28", session_id)

    assert updated is not None
    assert updated.updated_at is None


def test_update_returns_none_for_missing(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``update_session_policy`` returns ``None`` when the policy does not exist."""
    assert store.update("ef6cdebfba3f61098ef9d109f3a75a05", session_id, name="x") is None


def test_update_returns_none_for_wrong_session(
    store: SqlAlchemyPolicyStore,
    session_id: str,
    other_session_id: str,
) -> None:
    """``update_session_policy`` returns ``None`` for a different session."""
    store.create(
        policy_id="c7a1ffd25a9c5a71e2bccef623dabfcd",
        session_id=session_id,
        name="xsess_policy",
        type="python",
        handler="mod.func",
    )
    assert (
        store.update("c7a1ffd25a9c5a71e2bccef623dabfcd", other_session_id, enabled=False) is None
    )


# ── delete_session_policy ───────────────────────────────────────────────────


def test_delete_removes_policy(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``delete_session_policy`` removes the policy and returns ``True``."""
    store.create(
        policy_id="8b733f758f705ef3e4ca2595c24928ff",
        session_id=session_id,
        name="to_delete",
        type="python",
        handler="mod.func",
    )
    assert store.delete("8b733f758f705ef3e4ca2595c24928ff", session_id) is True
    assert store.get("8b733f758f705ef3e4ca2595c24928ff", session_id) is None


def test_delete_idempotent(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """``delete_session_policy`` on a missing policy returns ``False``."""
    assert store.delete("ef6cdebfba3f61098ef9d109f3a75a05", session_id) is False


def test_delete_wrong_session(
    store: SqlAlchemyPolicyStore,
    session_id: str,
    other_session_id: str,
) -> None:
    """``delete_session_policy`` returns ``False`` for a different session."""
    store.create(
        policy_id="280d6902ae146405886c3f44b6218ed8",
        session_id=session_id,
        name="xdel_policy",
        type="python",
        handler="mod.func",
    )
    assert store.delete("280d6902ae146405886c3f44b6218ed8", other_session_id) is False
    assert store.get("280d6902ae146405886c3f44b6218ed8", session_id) is not None


# ── Default (server-wide) policy methods ──────────────────────────────────────


def test_create_default_returns_policy(store: SqlAlchemyPolicyStore) -> None:
    """create_default inserts a server-wide policy with session_id=None."""
    policy = store.create_default(
        policy_id="d6e3889d86599a09d266a5ec5b5fb31f",
        name="default_block",
        type="python",
        handler="mod.default_handler",
    )
    assert policy.id == "d6e3889d86599a09d266a5ec5b5fb31f"
    assert policy.session_id is None
    assert policy.scope == "default"
    assert policy.name == "default_block"
    assert policy.type == "python"
    assert policy.handler == "mod.default_handler"
    assert policy.enabled is True
    assert policy.created_at > 0
    assert policy.updated_at is None


def test_create_default_with_factory_params(store: SqlAlchemyPolicyStore) -> None:
    """create_default stores factory_params as JSON."""
    policy = store.create_default(
        policy_id="9bcde4e26c73ca5a771e9348ef473825",
        name="parameterized",
        type="python",
        handler="mod.func",
        factory_params={"threshold": 0.5, "mode": "strict"},
    )
    assert policy.factory_params == {"threshold": 0.5, "mode": "strict"}


def test_create_default_with_created_by(store: SqlAlchemyPolicyStore) -> None:
    """create_default stores the created_by field."""
    policy = store.create_default(
        policy_id="f26ce3338728ddfb93a1a548d0ed03e5",
        name="audited",
        type="python",
        handler="mod.func",
        created_by="admin@example.com",
    )
    assert policy.created_by == "admin@example.com"


def test_create_default_duplicate_name_raises(store: SqlAlchemyPolicyStore) -> None:
    """create_default with a duplicate name raises IntegrityError."""
    store.create_default(
        policy_id="30ef9eac105234195d267465c6e27eff",
        name="unique_default",
        type="python",
        handler="mod.func",
    )
    with pytest.raises(IntegrityError):
        store.create_default(
            policy_id="b7355b4ee664fda76c12eea17a1d07ae",
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
        policy_id="28cb2620dd5d5ba3cb7560b76843cc03",
        session_id=session_id,
        name="shared_name",
        type="python",
        handler="mod.func",
    )
    default = store.create_default(
        policy_id="4897f0f26bbf333ebb62fe5ed5c6f199",
        name="shared_name",
        type="python",
        handler="mod.default_func",
    )
    assert default.session_id is None


def test_get_default_returns_policy(store: SqlAlchemyPolicyStore) -> None:
    """get_default fetches a default policy by ID."""
    store.create_default(
        policy_id="edac9ea84a69598e9ccb73c3336c8031",
        name="fetchable",
        type="python",
        handler="mod.func",
    )
    fetched = store.get_default("edac9ea84a69598e9ccb73c3336c8031")
    assert fetched is not None
    assert fetched.id == "edac9ea84a69598e9ccb73c3336c8031"
    assert fetched.name == "fetchable"


def test_get_default_returns_none_for_missing(store: SqlAlchemyPolicyStore) -> None:
    """get_default returns None when policy does not exist."""
    assert store.get_default("ab7b605870f3262e4176d537fff85e35") is None


def test_get_default_returns_none_for_session_policy(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """get_default returns None for a session-scoped policy."""
    store.create(
        policy_id="6a59a557f19ec1d10d08765fc49f4418",
        session_id=session_id,
        name="session_only",
        type="python",
        handler="mod.func",
    )
    assert store.get_default("6a59a557f19ec1d10d08765fc49f4418") is None


def test_list_defaults_returns_all_in_order(store: SqlAlchemyPolicyStore) -> None:
    """list_defaults returns all default policies ordered by created_at ASC."""
    store.create_default(
        policy_id="c403c06c83b3dc07d28c88443ec88e3d", name="first", type="python", handler="mod.a"
    )
    store.create_default(
        policy_id="e168997069dd3cb0a9863963a5a50e12", name="second", type="python", handler="mod.b"
    )
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
        policy_id="f3e2af0e0194687a8b55783bcab39d8a",
        session_id=session_id,
        name="session_pol",
        type="python",
        handler="mod.func",
    )
    store.create_default(
        policy_id="2bff8a0a808c93bc1ed430d4115029f2",
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
        policy_id="11f91e658f317b54d430ec7c45186f14",
        name="old_name",
        type="python",
        handler="mod.func",
    )
    updated = store.update_default("11f91e658f317b54d430ec7c45186f14", name="new_name")
    assert updated is not None
    assert updated.name == "new_name"
    assert updated.updated_at is not None


def test_update_default_changes_handler(store: SqlAlchemyPolicyStore) -> None:
    """update_default with handler= changes the handler."""
    store.create_default(
        policy_id="20bb4920c807fa9012956b59ae70c00f",
        name="handler_pol",
        type="python",
        handler="mod.old_func",
    )
    updated = store.update_default("20bb4920c807fa9012956b59ae70c00f", handler="mod.new_func")
    assert updated is not None
    assert updated.handler == "mod.new_func"


def test_update_default_changes_enabled(store: SqlAlchemyPolicyStore) -> None:
    """update_default with enabled=False disables the policy."""
    store.create_default(
        policy_id="9c22a1b2dfcfb077e9aedd1ef51c83f2",
        name="toggle_default",
        type="python",
        handler="mod.func",
    )
    updated = store.update_default("9c22a1b2dfcfb077e9aedd1ef51c83f2", enabled=False)
    assert updated is not None
    assert updated.enabled is False


def test_update_default_noop_does_not_bump_timestamp(store: SqlAlchemyPolicyStore) -> None:
    """update_default with no changes does not bump updated_at."""
    store.create_default(
        policy_id="f9af01a46b13c2b85c1f2c78ef637de2",
        name="noop_pol",
        type="python",
        handler="mod.func",
    )
    updated = store.update_default("f9af01a46b13c2b85c1f2c78ef637de2")
    assert updated is not None
    assert updated.updated_at is None


def test_update_default_returns_none_for_missing(store: SqlAlchemyPolicyStore) -> None:
    """update_default returns None when policy does not exist."""
    assert store.update_default("ab7b605870f3262e4176d537fff85e35", name="x") is None


def test_update_default_returns_none_for_session_policy(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """update_default returns None for a session-scoped policy."""
    store.create(
        policy_id="088193fca01626315b9eb95e3386d419",
        session_id=session_id,
        name="not_default",
        type="python",
        handler="mod.func",
    )
    assert store.update_default("088193fca01626315b9eb95e3386d419", name="new") is None


def test_update_default_duplicate_name_raises(store: SqlAlchemyPolicyStore) -> None:
    """update_default rejects a name that collides with another default."""
    store.create_default(
        policy_id="38ffbd2a7ed69c629bc17fb1b3f8ec04",
        name="name_a",
        type="python",
        handler="mod.func",
    )
    store.create_default(
        policy_id="c3d271f156340112c2b47aeeedabbe92",
        name="name_b",
        type="python",
        handler="mod.func",
    )
    with pytest.raises(IntegrityError):
        store.update_default("c3d271f156340112c2b47aeeedabbe92", name="name_a")


def test_delete_default_removes_policy(store: SqlAlchemyPolicyStore) -> None:
    """delete_default removes the policy and returns True."""
    store.create_default(
        policy_id="7b26ccf04a2a0b3d35d7df2644362cef",
        name="to_delete",
        type="python",
        handler="mod.func",
    )
    assert store.delete_default("7b26ccf04a2a0b3d35d7df2644362cef") is True
    assert store.get_default("7b26ccf04a2a0b3d35d7df2644362cef") is None


def test_delete_default_idempotent(store: SqlAlchemyPolicyStore) -> None:
    """delete_default on a missing policy returns False."""
    assert store.delete_default("ab7b605870f3262e4176d537fff85e35") is False


def test_delete_default_rejects_session_policy(
    store: SqlAlchemyPolicyStore,
    session_id: str,
) -> None:
    """delete_default returns False for a session-scoped policy."""
    store.create(
        policy_id="8e2f6fb3fda2fb90e2c97fcb29940858",
        session_id=session_id,
        name="cant_delete_as_default",
        type="python",
        handler="mod.func",
    )
    assert store.delete_default("8e2f6fb3fda2fb90e2c97fcb29940858") is False
