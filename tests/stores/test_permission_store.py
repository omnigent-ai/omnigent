"""Tests for :class:`SqlAlchemyPermissionStore`.

Exercises all public methods against a real SQLite database
(migrations applied via :func:`get_or_create_engine`), following the
same pattern used by :mod:`tests.stores.test_comment_store`.

The ``db_uri`` fixture in the root conftest creates a fresh per-test
SQLite file and tears it down automatically.
"""

from __future__ import annotations

import pytest

from omnigent.entities import SessionPermission
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyPermissionStore:
    """A fresh :class:`SqlAlchemyPermissionStore` backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemyPermissionStore` instance.
    """
    return SqlAlchemyPermissionStore(db_uri)


# ── helpers ──────────────────────────────────────────────────────────────────


def _ensure_user(store: SqlAlchemyPermissionStore, user_id: str) -> None:
    """Create a user row so FK constraints on session_permissions are satisfied.

    :param store: The permission store to use.
    :param user_id: The user identifier to ensure, e.g. ``"alice@test.com"``.
    """
    store.ensure_user(user_id)


def _create_conversation(db_uri: str) -> str:
    """Create a conversation and return its id.

    Needed because ``session_permissions.conversation_id`` has a FK to
    ``conversations.id``.

    :param db_uri: SQLite URI for the conversation store.
    :returns: The id of the newly created conversation.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    return conv.id


# ── grant ────────────────────────────────────────────────────────────────────


def test_grant_creates_permission(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """``grant`` creates a new permission row and returns a SessionPermission.

    If this fails, the INSERT or upsert path is broken or the returned
    entity does not match the input values.
    """
    _ensure_user(store, "alice@test.com")
    conv_id = _create_conversation(db_uri)

    result = store.grant("alice@test.com", conv_id, level=2)

    # The returned entity must be a real SessionPermission with correct values.
    assert isinstance(result, SessionPermission), (
        f"Expected SessionPermission, got {type(result).__name__}. "
        "grant() must return the domain entity, not the ORM row."
    )
    assert result.user_id == "alice@test.com", (
        f"user_id mismatch: expected 'alice@test.com', got {result.user_id!r}"
    )
    assert result.conversation_id == conv_id, (
        f"conversation_id mismatch: expected {conv_id!r}, got {result.conversation_id!r}"
    )
    # level=2 means edit access.
    assert result.level == 2, (
        f"Expected level 2 (edit), got {result.level}. "
        "The level was not written or returned correctly."
    )


def test_grant_is_persisted_and_retrievable(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """A grant created by ``grant`` is immediately visible via ``get``.

    Confirms the row was actually written to the DB, not just returned
    in-memory.
    """
    _ensure_user(store, "bob@test.com")
    conv_id = _create_conversation(db_uri)

    store.grant("bob@test.com", conv_id, level=1)
    fetched = store.get("bob@test.com", conv_id)

    # The grant must be retrievable after creation.
    assert fetched is not None, (
        "get() returned None for a grant that was just created. "
        "The row was not persisted or get() is not querying correctly."
    )
    assert fetched.user_id == "bob@test.com"
    assert fetched.conversation_id == conv_id
    assert fetched.level == 1, (
        f"Expected level 1 (read), got {fetched.level}. The level was persisted incorrectly."
    )


def test_grant_upsert_upgrades_level(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """Granting to the same (user, session) pair overwrites the level upward.

    The upsert must update the existing row, not fail with a unique
    constraint violation or silently ignore the new level.
    """
    _ensure_user(store, "carol@test.com")
    conv_id = _create_conversation(db_uri)

    store.grant("carol@test.com", conv_id, level=1)
    upgraded = store.grant("carol@test.com", conv_id, level=3)

    # The returned entity must reflect the new level.
    assert upgraded.level == 3, (
        f"Expected level 3 (manage) after upgrade, got {upgraded.level}. "
        "The upsert ON CONFLICT DO UPDATE is not overwriting the level."
    )

    # Also verify via get() to confirm persistence.
    fetched = store.get("carol@test.com", conv_id)
    assert fetched is not None
    assert fetched.level == 3, (
        f"Expected persisted level 3, got {fetched.level}. "
        "The upsert returned the new level but did not persist it."
    )


def test_grant_upsert_downgrades_level(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """Granting to the same (user, session) pair can also downgrade the level.

    The ABC contract says "can upgrade AND downgrade" -- this verifies
    the downgrade direction.
    """
    _ensure_user(store, "dave@test.com")
    conv_id = _create_conversation(db_uri)

    store.grant("dave@test.com", conv_id, level=3)
    downgraded = store.grant("dave@test.com", conv_id, level=1)

    assert downgraded.level == 1, (
        f"Expected level 1 (read) after downgrade, got {downgraded.level}. "
        "The upsert is not overwriting the level on downgrade."
    )

    fetched = store.get("dave@test.com", conv_id)
    assert fetched is not None
    assert fetched.level == 1, f"Expected persisted level 1 after downgrade, got {fetched.level}."


def test_grant_with_public_sentinel(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """``grant`` with the ``__public__`` sentinel user_id works like any other user.

    The __public__ sentinel represents public read access. It must be
    treatable as a normal user_id from the store's perspective.
    """
    _ensure_user(store, "__public__")
    conv_id = _create_conversation(db_uri)

    result = store.grant("__public__", conv_id, level=1)

    assert result.user_id == "__public__", f"Expected user_id '__public__', got {result.user_id!r}"
    assert result.level == 1

    # Verify it round-trips through get().
    fetched = store.get("__public__", conv_id)
    assert fetched is not None, (
        "get() returned None for __public__ grant. "
        "The __public__ sentinel is not persisting as a normal user_id."
    )
    assert fetched.level == 1


# ── revoke ───────────────────────────────────────────────────────────────────


def test_revoke_removes_grant(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """``revoke`` removes the permission row and returns True.

    After revocation, ``get`` must return None for the same (user, session).
    """
    _ensure_user(store, "eve@test.com")
    conv_id = _create_conversation(db_uri)
    store.grant("eve@test.com", conv_id, level=2)

    removed = store.revoke("eve@test.com", conv_id)

    # Must return True when a row was actually deleted.
    assert removed is True, (
        f"Expected True from revoke (row existed), got {removed!r}. "
        "The DELETE did not match the row or rowcount is wrong."
    )

    # The grant must no longer be retrievable.
    fetched = store.get("eve@test.com", conv_id)
    assert fetched is None, (
        f"Expected None after revoke, got {fetched!r}. The row was not actually deleted."
    )


def test_revoke_nonexistent_returns_false(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """``revoke`` returns False when no matching grant exists.

    Must not raise an exception -- the contract says no-op + False.
    """
    conv_id = _create_conversation(db_uri)

    result = store.revoke("nobody@test.com", conv_id)

    assert result is False, (
        f"Expected False for revoking a nonexistent grant, got {result!r}. "
        "revoke() should return False, not raise, when no row matches."
    )


# ── get ──────────────────────────────────────────────────────────────────────


def test_get_returns_grant_if_exists(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """``get`` returns the SessionPermission for an existing grant."""
    _ensure_user(store, "fay@test.com")
    conv_id = _create_conversation(db_uri)
    store.grant("fay@test.com", conv_id, level=3)

    fetched = store.get("fay@test.com", conv_id)

    assert fetched is not None, "get() returned None for an existing grant"
    assert isinstance(fetched, SessionPermission), (
        f"Expected SessionPermission, got {type(fetched).__name__}"
    )
    assert fetched.user_id == "fay@test.com"
    assert fetched.conversation_id == conv_id
    assert fetched.level == 3, (
        f"Expected level 3 (manage), got {fetched.level}. "
        "The level was not read correctly from the DB."
    )


def test_get_returns_none_for_missing(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """``get`` returns None when no grant exists for the (user, session) pair."""
    conv_id = _create_conversation(db_uri)

    result = store.get("ghost@test.com", conv_id)

    assert result is None, (
        f"Expected None for a nonexistent grant, got {result!r}. "
        "get() should return None, not raise, for missing grants."
    )


# ── list_for_session ─────────────────────────────────────────────────────────


def test_list_for_session_returns_all_grants(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """``list_for_session`` returns all grants on a given session."""
    _ensure_user(store, "alice@test.com")
    _ensure_user(store, "bob@test.com")
    conv_id = _create_conversation(db_uri)

    store.grant("alice@test.com", conv_id, level=1)
    store.grant("bob@test.com", conv_id, level=3)

    grants = store.list_for_session(conv_id)

    # Both grants must be present.
    assert len(grants) == 2, (
        f"Expected 2 grants for session, got {len(grants)}. "
        "list_for_session is not returning all grants."
    )
    user_ids = {g.user_id for g in grants}
    assert user_ids == {"alice@test.com", "bob@test.com"}, (
        f"Expected users alice and bob, got {user_ids}"
    )
    # Verify content, not just structure.
    by_user = {g.user_id: g for g in grants}
    assert by_user["alice@test.com"].level == 1
    assert by_user["bob@test.com"].level == 3


def test_list_for_session_empty(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """``list_for_session`` returns [] for a session with no grants."""
    conv_id = _create_conversation(db_uri)

    result = store.list_for_session(conv_id)

    assert result == [], f"Expected [] for a session with no grants, got {result!r}"


def test_list_for_session_isolation(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """Grants on session A are invisible to ``list_for_session`` for session B.

    The conversation_id must act as an isolation boundary.
    """
    _ensure_user(store, "alice@test.com")
    conv_a = _create_conversation(db_uri)
    conv_b = _create_conversation(db_uri)

    store.grant("alice@test.com", conv_a, level=2)

    b_grants = store.list_for_session(conv_b)
    assert b_grants == [], (
        f"Expected no grants for conv_b, got {b_grants}. "
        "Grants are leaking across session boundaries."
    )


# ── list_for_user ────────────────────────────────────────────────────────────


def test_list_for_user_returns_all_grants(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """``list_for_user`` returns all grants for a given user across sessions."""
    _ensure_user(store, "alice@test.com")
    conv_1 = _create_conversation(db_uri)
    conv_2 = _create_conversation(db_uri)

    store.grant("alice@test.com", conv_1, level=1)
    store.grant("alice@test.com", conv_2, level=3)

    grants = store.list_for_user("alice@test.com")

    # Alice has 2 grants, one per session.
    assert len(grants) == 2, (
        f"Expected 2 grants for alice, got {len(grants)}. "
        "list_for_user is not returning all grants."
    )
    conv_ids = {g.conversation_id for g in grants}
    assert conv_ids == {conv_1, conv_2}, (
        f"Expected conversation ids {conv_1!r} and {conv_2!r}, got {conv_ids}"
    )


def test_list_for_user_empty(store: SqlAlchemyPermissionStore) -> None:
    """``list_for_user`` returns [] for a user with no grants."""
    result = store.list_for_user("nobody@test.com")

    assert result == [], f"Expected [] for a user with no grants, got {result!r}"


def test_list_for_user_isolation(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """Grants for user A are invisible to ``list_for_user`` for user B.

    The user_id must act as an isolation boundary.
    """
    _ensure_user(store, "alice@test.com")
    _ensure_user(store, "bob@test.com")
    conv_id = _create_conversation(db_uri)

    store.grant("alice@test.com", conv_id, level=2)

    bob_grants = store.list_for_user("bob@test.com")
    assert bob_grants == [], (
        f"Expected no grants for bob, got {bob_grants}. Grants are leaking across user boundaries."
    )


# ── ensure_user ──────────────────────────────────────────────────────────────


def test_ensure_user_creates_user(store: SqlAlchemyPermissionStore) -> None:
    """``ensure_user`` creates a user row if the user does not exist.

    After calling ensure_user, the user must be visible to is_admin.
    """
    store.ensure_user("newuser@test.com")

    # Non-admin user: is_admin should return False.
    assert store.is_admin("newuser@test.com") is False, (
        "Expected is_admin=False for a regular user created by ensure_user"
    )


def test_ensure_user_is_idempotent(store: SqlAlchemyPermissionStore) -> None:
    """Calling ``ensure_user`` twice for the same user_id does not raise.

    The upsert must be a no-op on the second call, not a unique constraint
    violation.
    """
    store.ensure_user("repeat@test.com")
    # Second call must not raise.
    store.ensure_user("repeat@test.com")

    # The user must still exist and be non-admin.
    assert store.is_admin("repeat@test.com") is False


def test_ensure_user_with_admin_flag(store: SqlAlchemyPermissionStore) -> None:
    """``ensure_user`` with ``is_admin=True`` creates an admin user.

    The admin flag must be visible via ``is_admin()``.
    """
    store.ensure_user("admin@test.com", is_admin=True)

    assert store.is_admin("admin@test.com") is True, (
        "Expected is_admin=True for a user created with is_admin=True"
    )


def test_ensure_user_does_not_overwrite_admin_flag(
    store: SqlAlchemyPermissionStore,
) -> None:
    """Calling ``ensure_user(is_admin=False)`` after an admin was created preserves admin.

    The upsert uses ON CONFLICT DO NOTHING, so a second call with
    different is_admin must not overwrite the existing row.
    """
    store.ensure_user("local", is_admin=True)
    # Second call with default is_admin=False.
    store.ensure_user("local")

    # The admin flag must be preserved from the first call.
    assert store.is_admin("local") is True, (
        "Expected is_admin=True to be preserved. "
        "ensure_user ON CONFLICT DO NOTHING must not overwrite the admin flag."
    )


# ── list_users ───────────────────────────────────────────────────────────────


def test_list_users_returns_real_users_with_admin_flag(
    store: SqlAlchemyPermissionStore,
) -> None:
    """``list_users`` returns every real user with the admin flag set.

    Backs the OIDC/header admin user list — the analog of the
    accounts-mode ``account_store.list_users()``.
    """
    store.ensure_user("alice@test.com", is_admin=True)
    store.ensure_user("bob@test.com")

    users = {u.id: u for u in store.list_users()}
    assert set(users) == {"alice@test.com", "bob@test.com"}
    assert users["alice@test.com"].is_admin is True
    assert users["bob@test.com"].is_admin is False
    # Header/OIDC rows have no password — the Account shape must reflect that.
    assert users["bob@test.com"].has_password is False


def test_list_users_excludes_reserved_sentinels(
    store: SqlAlchemyPermissionStore,
) -> None:
    """``list_users`` hides the ``local`` and ``__public__`` sentinels.

    They aren't real, actionable actors, matching
    ``account_store.list_users()`` so the admin list is identical
    across auth modes.
    """
    store.ensure_user("local", is_admin=True)
    store.ensure_user("__public__")
    store.ensure_user("real@test.com")

    ids = {u.id for u in store.list_users()}
    assert ids == {"real@test.com"}


def test_list_users_empty(store: SqlAlchemyPermissionStore) -> None:
    """``list_users`` returns an empty list when there are no real users."""
    assert store.list_users() == []


# ── is_admin ─────────────────────────────────────────────────────────────────


def test_is_admin_true_for_admin_user(store: SqlAlchemyPermissionStore) -> None:
    """``is_admin`` returns True for a user with the admin flag set."""
    store.ensure_user("superadmin@test.com", is_admin=True)

    assert store.is_admin("superadmin@test.com") is True


def test_is_admin_false_for_regular_user(store: SqlAlchemyPermissionStore) -> None:
    """``is_admin`` returns False for a user without the admin flag."""
    store.ensure_user("regular@test.com")

    assert store.is_admin("regular@test.com") is False


def test_is_admin_false_for_nonexistent_user(store: SqlAlchemyPermissionStore) -> None:
    """``is_admin`` returns False for a user_id that does not exist in the DB.

    Must not raise -- the contract says "False for nonexistent."
    """
    assert store.is_admin("doesnotexist@test.com") is False, (
        "Expected False for a nonexistent user, not an exception."
    )


# ── has_any_grants ───────────────────────────────────────────────────────────


def test_has_any_grants_true_when_grants_exist(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """``has_any_grants`` returns True when at least one grant exists on the session."""
    _ensure_user(store, "alice@test.com")
    conv_id = _create_conversation(db_uri)

    store.grant("alice@test.com", conv_id, level=1)

    assert store.has_any_grants(conv_id) is True, (
        "Expected True after granting a permission, got False. "
        "The EXISTS query is not finding the row."
    )


def test_has_any_grants_false_when_no_grants(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """``has_any_grants`` returns False when no grants exist on the session."""
    conv_id = _create_conversation(db_uri)

    assert store.has_any_grants(conv_id) is False, (
        "Expected False for a session with no grants, got True."
    )


def test_has_any_grants_false_after_revoke(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """``has_any_grants`` returns False after the only grant is revoked.

    Verifies that revoke actually removes the row so the EXISTS query
    correctly returns False.
    """
    _ensure_user(store, "alice@test.com")
    conv_id = _create_conversation(db_uri)

    store.grant("alice@test.com", conv_id, level=1)
    store.revoke("alice@test.com", conv_id)

    assert store.has_any_grants(conv_id) is False, (
        "Expected False after revoking the only grant, got True. "
        "The revoke did not delete the row."
    )


# ── cascade delete ───────────────────────────────────────────────────────────


def test_permissions_not_auto_deleted_when_conversation_deleted(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """Without DB FK cascade, deleting a conversation leaves its permission rows intact.

    The application (delete_conversation) is responsible for explicitly
    deleting session_permissions rows when a conversation is removed.
    """
    _ensure_user(store, "alice@test.com")
    _ensure_user(store, "bob@test.com")
    conv_id = _create_conversation(db_uri)

    store.grant("alice@test.com", conv_id, level=2)
    store.grant("bob@test.com", conv_id, level=1)

    assert store.has_any_grants(conv_id) is True, "Pre-condition: grants must exist"

    # Delete the conversation directly — no FK cascade fires.
    from sqlalchemy import delete as sa_delete

    from omnigent.db.db_models import SqlConversation
    from omnigent.db.utils import get_or_create_engine, make_managed_session_maker

    engine = get_or_create_engine(db_uri)
    session_maker = make_managed_session_maker(engine)
    with session_maker() as session:
        session.execute(sa_delete(SqlConversation).where(SqlConversation.id == conv_id))

    # Grants remain — the application must clean them up explicitly.
    assert store.has_any_grants(conv_id) is True, (
        "Without FK cascade, permission rows must persist after conversation deletion."
    )


def test_cascade_delete_does_not_affect_other_sessions(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """CASCADE delete of one conversation does not remove grants on another.

    Grants on session B must survive when session A is deleted.
    """
    _ensure_user(store, "alice@test.com")
    conv_a = _create_conversation(db_uri)
    conv_b = _create_conversation(db_uri)

    store.grant("alice@test.com", conv_a, level=1)
    store.grant("alice@test.com", conv_b, level=2)

    # Delete conv_a.
    from sqlalchemy import delete as sa_delete

    from omnigent.db.db_models import SqlConversation
    from omnigent.db.utils import get_or_create_engine, make_managed_session_maker

    engine = get_or_create_engine(db_uri)
    session_maker = make_managed_session_maker(engine)
    with session_maker() as session:
        session.execute(sa_delete(SqlConversation).where(SqlConversation.id == conv_a))

    # conv_b's grant must survive.
    fetched = store.get("alice@test.com", conv_b)
    assert fetched is not None, (
        "Expected grant on conv_b to survive after deleting conv_a, but get() returned None."
    )
    assert fetched.level == 2


# ── list_conversations accessible_by filter ──────────────────────────────────


def test_list_conversations_user_with_direct_grant_sees_session(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """A user with a direct grant sees their session via ``list_conversations(accessible_by=...)``.

    The UNION filter in list_conversations must include sessions where the
    user has a direct permission row.
    """
    _ensure_user(store, "alice@test.com")
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()

    store.grant("alice@test.com", conv.id, level=2)

    page = conv_store.list_conversations(
        accessible_by="alice@test.com",
        kind=None,
    )

    # Alice has a direct grant, so the session must appear.
    conv_ids = {c.id for c in page.data}
    assert conv.id in conv_ids, (
        f"Expected conv {conv.id!r} to be visible to alice via direct grant, "
        f"but list_conversations returned {conv_ids}."
    )


def test_list_conversations_user_with_no_grants_sees_nothing(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """A user with no grants sees no sessions via ``list_conversations(accessible_by=...)``.

    The UNION filter must exclude sessions where the user has no permission row.
    """
    _ensure_user(store, "alice@test.com")
    _ensure_user(store, "bob@test.com")
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()

    # Only alice has a grant.
    store.grant("alice@test.com", conv.id, level=2)

    page = conv_store.list_conversations(
        accessible_by="bob@test.com",
        kind=None,
    )

    # Bob has no grants -- he must see nothing.
    conv_ids = {c.id for c in page.data}
    assert conv.id not in conv_ids, (
        f"Expected conv {conv.id!r} to be invisible to bob (no grant), "
        f"but list_conversations returned it."
    )


def test_list_conversations_public_only_grants_hidden_from_sidebar(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """Sessions with only a ``__public__`` grant are NOT listed for other users.

    The ``accessible_by`` filter only matches direct user grants so that
    public-only sessions don't clutter every user's sidebar.  Public
    sessions remain accessible by direct URL — they just aren't listed.
    """
    _ensure_user(store, "__public__")
    _ensure_user(store, "stranger@test.com")
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()

    store.grant("__public__", conv.id, level=1)

    page = conv_store.list_conversations(
        accessible_by="stranger@test.com",
        kind=None,
    )

    conv_ids = {c.id for c in page.data}
    assert conv.id not in conv_ids, (
        f"Expected conv {conv.id!r} to be hidden from stranger (public-only grant), "
        f"but list_conversations returned it."
    )


def test_list_conversations_multiple_users_see_correct_sessions(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """Multiple users with different grants see only their own sessions.

    Alice sees conv_a and conv_shared. Bob sees conv_b and conv_shared.
    Neither sees the other's private session.
    """
    _ensure_user(store, "alice@test.com")
    _ensure_user(store, "bob@test.com")
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_a = conv_store.create_conversation()
    conv_b = conv_store.create_conversation()
    conv_shared = conv_store.create_conversation()

    store.grant("alice@test.com", conv_a.id, level=3)
    store.grant("bob@test.com", conv_b.id, level=3)
    store.grant("alice@test.com", conv_shared.id, level=2)
    store.grant("bob@test.com", conv_shared.id, level=1)

    alice_page = conv_store.list_conversations(
        accessible_by="alice@test.com",
        kind=None,
    )
    bob_page = conv_store.list_conversations(
        accessible_by="bob@test.com",
        kind=None,
    )

    alice_ids = {c.id for c in alice_page.data}
    bob_ids = {c.id for c in bob_page.data}

    # Alice sees conv_a and conv_shared.
    assert conv_a.id in alice_ids, "Alice must see her private session conv_a"
    assert conv_shared.id in alice_ids, "Alice must see conv_shared"
    assert conv_b.id not in alice_ids, "Alice must NOT see Bob's private session conv_b"

    # Bob sees conv_b and conv_shared.
    assert conv_b.id in bob_ids, "Bob must see his private session conv_b"
    assert conv_shared.id in bob_ids, "Bob must see conv_shared"
    assert conv_a.id not in bob_ids, "Bob must NOT see Alice's private session conv_a"


def test_list_conversations_direct_grant_required_public_alone_hidden(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """Only sessions with a direct user grant appear; public-only sessions are hidden.

    ``conv_direct`` and ``conv_both`` have Alice-specific grants and must
    appear. ``conv_public`` has only a ``__public__`` grant and must NOT
    appear — the sidebar only shows sessions the user explicitly has
    access to.
    """
    _ensure_user(store, "alice@test.com")
    _ensure_user(store, "__public__")
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_direct = conv_store.create_conversation()
    conv_public = conv_store.create_conversation()
    conv_both = conv_store.create_conversation()

    store.grant("alice@test.com", conv_direct.id, level=2)
    store.grant("__public__", conv_public.id, level=1)
    store.grant("alice@test.com", conv_both.id, level=3)
    store.grant("__public__", conv_both.id, level=1)

    page = conv_store.list_conversations(
        accessible_by="alice@test.com",
        kind=None,
    )

    visible_ids = {c.id for c in page.data}

    assert conv_direct.id in visible_ids, "Alice must see conv_direct (direct grant)"
    assert conv_public.id not in visible_ids, (
        "Alice must NOT see conv_public (public-only, no direct grant)"
    )
    assert conv_both.id in visible_ids, (
        "Alice must see conv_both (has a direct grant alongside the public one)"
    )


# ── resolve_access ───────────────────────────────────────────────────────────


def test_resolve_access_direct_grant_only(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """``resolve_access`` reports the user's own grant and no public grant.

    Proves the bundled read returns the same data the separate
    ``is_admin`` + ``get`` calls would, so callers can derive both the
    access decision and the displayed level from one round-trip.
    """
    _ensure_user(store, "alice@test.com")
    conv_id = _create_conversation(db_uri)
    store.grant("alice@test.com", conv_id, level=2)

    resolved = store.resolve_access("alice@test.com", conv_id)

    assert resolved.is_admin is False, "alice is not an admin"
    # The user's direct grant is edit (2); no __public__ grant exists.
    assert resolved.user_grant_level == 2, (
        f"expected user_grant_level 2, got {resolved.user_grant_level}"
    )
    assert resolved.public_grant_level is None, (
        f"expected no public grant, got {resolved.public_grant_level}"
    )


def test_resolve_access_separates_user_and_public_grants(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """Both the user grant and a differing ``__public__`` grant are returned.

    This is the case that distinguishes access (satisfied by either) from
    the displayed level (prefers the user's own grant): a low user grant
    alongside a higher public grant must surface BOTH levels so the policy
    layer can apply each rule correctly.
    """
    _ensure_user(store, "alice@test.com")
    _ensure_user(store, "__public__")
    conv_id = _create_conversation(db_uri)
    store.grant("alice@test.com", conv_id, level=1)  # user: read
    store.grant("__public__", conv_id, level=3)  # public: manage

    resolved = store.resolve_access("alice@test.com", conv_id)

    assert resolved.is_admin is False
    assert resolved.user_grant_level == 1, (
        f"user's own grant must be read (1), got {resolved.user_grant_level}"
    )
    assert resolved.public_grant_level == 3, (
        f"public grant must be manage (3), got {resolved.public_grant_level}"
    )


def test_resolve_access_public_grant_only(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """A user with no own grant but a ``__public__`` grant surfaces only public.

    Pins the public-fallback path at the store layer: when the caller has
    no direct grant, ``user_grant_level`` is ``None`` while the
    ``__public__`` grant level is still reported, so the policy layer can
    allow access (and display the public level) off this single read.
    """
    _ensure_user(store, "stranger@test.com")
    _ensure_user(store, "__public__")
    conv_id = _create_conversation(db_uri)
    store.grant("__public__", conv_id, level=1)  # public: read

    resolved = store.resolve_access("stranger@test.com", conv_id)

    assert resolved.is_admin is False
    assert resolved.user_grant_level is None, (
        f"caller has no direct grant, got {resolved.user_grant_level}"
    )
    assert resolved.public_grant_level == 1, (
        f"public grant must be read (1), got {resolved.public_grant_level}"
    )


def test_resolve_access_admin_flag(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """``resolve_access`` reflects the admin flag with no grants present.

    An admin with no explicit grant must still be reported as admin so the
    policy layer can apply the admin bypass.
    """
    store.ensure_user("root@test.com", is_admin=True)
    conv_id = _create_conversation(db_uri)

    resolved = store.resolve_access("root@test.com", conv_id)

    assert resolved.is_admin is True, "admin flag must be reported"
    assert resolved.user_grant_level is None, "admin has no explicit grant here"
    assert resolved.public_grant_level is None


def test_resolve_access_no_grants_no_admin(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """A user with no grant and no admin flag resolves to all-empty.

    This is the deny case: every field falsy so the policy layer denies
    (and the route 404s to avoid leaking session existence).
    """
    _ensure_user(store, "stranger@test.com")
    conv_id = _create_conversation(db_uri)

    resolved = store.resolve_access("stranger@test.com", conv_id)

    assert resolved.is_admin is False
    assert resolved.user_grant_level is None
    assert resolved.public_grant_level is None


def test_resolve_access_none_user(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """``resolve_access(None, ...)`` short-circuits to an all-empty snapshot.

    Unauthenticated callers never touch the DB and resolve to no access.
    """
    conv_id = _create_conversation(db_uri)

    resolved = store.resolve_access(None, conv_id)

    assert resolved.is_admin is False
    assert resolved.user_grant_level is None
    assert resolved.public_grant_level is None


# ── reassign_user_grants (local→admin migration on first-run accounts setup) ──


def test_reassign_user_grants_moves_grants_to_target(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """All of the source user's grants move to the target user.

    The single-user-local continuity case: the reserved ``local`` user's
    pre-accounts sessions are handed to the freshly-created admin.
    """
    _ensure_user(store, "local")
    conv_a = _create_conversation(db_uri)
    conv_b = _create_conversation(db_uri)
    store.grant("local", conv_a, level=2)
    store.grant("local", conv_b, level=2)

    moved = store.reassign_user_grants("local", "alice")

    assert moved == 2
    # Both sessions are now the admin's; none remain under "local".
    assert {p.conversation_id for p in store.list_for_user("alice")} == {conv_a, conv_b}
    assert store.list_for_user("local") == []
    # The destination user row was created so the FK held; reassign does not
    # set admin (that's the caller's job via ensure_user), so it's non-admin.
    assert store.is_admin("alice") is False


def test_reassign_user_grants_dedups_when_target_already_has_grant(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """A conversation the target already holds isn't duplicated; the source
    grant is dropped and not counted as moved."""
    _ensure_user(store, "local")
    _ensure_user(store, "alice")
    conv = _create_conversation(db_uri)
    store.grant("local", conv, level=2)
    store.grant("alice", conv, level=2)

    moved = store.reassign_user_grants("local", "alice")

    assert moved == 0  # already present → dropped, not moved
    assert [p.conversation_id for p in store.list_for_user("alice")] == [conv]
    assert store.list_for_user("local") == []


# ── check_access ──────────────────────────────────────────────────────────────


def test_check_access_direct_grant(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """check_access returns True when user has a direct grant at or above required level."""
    _ensure_user(store, "alice")
    conv = _create_conversation(db_uri)
    store.grant("alice", conv, level=2)

    assert store.check_access("alice", conv, required_level=1) is True
    assert store.check_access("alice", conv, required_level=2) is True
    assert store.check_access("alice", conv, required_level=3) is False


def test_check_access_public_grant_fallback(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """check_access falls back to __public__ grant when user has no direct grant."""
    from omnigent.server.auth import RESERVED_USER_PUBLIC

    _ensure_user(store, "alice")
    _ensure_user(store, RESERVED_USER_PUBLIC)
    conv = _create_conversation(db_uri)
    store.grant(RESERVED_USER_PUBLIC, conv, level=1)

    assert store.check_access("alice", conv, required_level=1) is True
    assert store.check_access("alice", conv, required_level=2) is False


def test_check_access_none_user(store: SqlAlchemyPermissionStore) -> None:
    """check_access returns False when user_id is None."""
    assert store.check_access(None, "conv_any", required_level=1) is False


def test_check_access_no_grants(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """check_access returns False when user has no grants and no public access."""
    _ensure_user(store, "alice")
    conv = _create_conversation(db_uri)
    assert store.check_access("alice", conv, required_level=1) is False


# ── get_permission_level ──────────────────────────────────────────────────────


def test_get_permission_level_direct_grant(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """get_permission_level returns the user's direct grant level."""
    _ensure_user(store, "alice")
    conv = _create_conversation(db_uri)
    store.grant("alice", conv, level=3)

    assert store.get_permission_level("alice", conv) == 3


def test_get_permission_level_admin_gets_owner(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """get_permission_level returns LEVEL_OWNER for admin users."""
    from omnigent.server.auth import LEVEL_OWNER

    store.ensure_user("admin_user", is_admin=True)
    conv = _create_conversation(db_uri)

    assert store.get_permission_level("admin_user", conv) == LEVEL_OWNER


def test_get_permission_level_public_fallback(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """get_permission_level falls back to public grant when no direct grant."""
    from omnigent.server.auth import RESERVED_USER_PUBLIC

    _ensure_user(store, "alice")
    _ensure_user(store, RESERVED_USER_PUBLIC)
    conv = _create_conversation(db_uri)
    store.grant(RESERVED_USER_PUBLIC, conv, level=1)

    assert store.get_permission_level("alice", conv) == 1


def test_get_permission_level_none_user(store: SqlAlchemyPermissionStore) -> None:
    """get_permission_level returns None for None user_id."""
    assert store.get_permission_level(None, "conv_any") is None


def test_get_permission_level_no_grants(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """get_permission_level returns None when no grants exist."""
    _ensure_user(store, "alice")
    conv = _create_conversation(db_uri)
    assert store.get_permission_level("alice", conv) is None


# ── set_admin ─────────────────────────────────────────────────────────────────


def test_set_admin_promotes_user(store: SqlAlchemyPermissionStore) -> None:
    """set_admin(user, True) makes the user an admin."""
    store.ensure_user("bob")
    assert store.is_admin("bob") is False
    store.set_admin("bob", True)
    assert store.is_admin("bob") is True


def test_set_admin_demotes_user(store: SqlAlchemyPermissionStore) -> None:
    """set_admin(user, False) removes admin status."""
    store.ensure_user("carol", is_admin=True)
    assert store.is_admin("carol") is True
    store.set_admin("carol", False)
    assert store.is_admin("carol") is False


# ── list_for_sessions (bulk) ──────────────────────────────────────────────────


def test_list_for_sessions_returns_grouped_grants(
    store: SqlAlchemyPermissionStore, db_uri: str
) -> None:
    """list_for_sessions returns grants grouped by conversation_id."""
    _ensure_user(store, "alice")
    _ensure_user(store, "bob")
    conv_a = _create_conversation(db_uri)
    conv_b = _create_conversation(db_uri)
    store.grant("alice", conv_a, level=2)
    store.grant("bob", conv_a, level=1)
    store.grant("alice", conv_b, level=3)

    result = store.list_for_sessions([conv_a, conv_b])
    assert len(result[conv_a]) == 2
    assert len(result[conv_b]) == 1
    assert result[conv_b][0].user_id == "alice"


def test_list_for_sessions_empty_input(store: SqlAlchemyPermissionStore) -> None:
    """list_for_sessions with empty list returns empty dict."""
    assert store.list_for_sessions([]) == {}


def test_list_for_sessions_no_grants(store: SqlAlchemyPermissionStore, db_uri: str) -> None:
    """list_for_sessions returns empty lists for conversations with no grants."""
    conv = _create_conversation(db_uri)
    result = store.list_for_sessions([conv])
    assert result == {conv: []}
