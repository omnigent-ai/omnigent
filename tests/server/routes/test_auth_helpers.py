"""Tests for the combined permission helper in ``_auth_helpers``.

Focused on :func:`require_access_and_level`, which folds ``require_access``
and ``get_permission_level`` into a single resolution. The behaviour it must
preserve is the 403-vs-404 distinction, the admin bypass, sub-agent parent
delegation, and the user-vs-public displayed-level asymmetry — all exercised
here against real SQLite-backed stores (no mocks) so the resolution matches
production exactly.
"""

from __future__ import annotations

import pytest

from omnigent.entities.agent import Agent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
)
from omnigent.server.routes import _auth_helpers
from omnigent.server.routes._auth_helpers import require_access_and_level, require_agent_owner
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)

ALICE = "alice@test.com"
BOB = "bob@test.com"


def _session_agent(
    created_by: str | None, session_id: str = "s0000000000000000000000000000001"
) -> Agent:
    """Build a session-scoped :class:`Agent` for owner-check tests."""
    return Agent(
        id="a0000000000000000000000000000009",
        created_at=1,
        name="scoped",
        bundle_location="ag/loc",
        session_id=session_id,
        created_by=created_by,
    )


@pytest.fixture()
def perm_store(db_uri: str) -> SqlAlchemyPermissionStore:
    """A fresh permission store on the per-test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest.
    :returns: A ready :class:`SqlAlchemyPermissionStore`.
    """
    return SqlAlchemyPermissionStore(db_uri)


@pytest.fixture()
def conv_store(db_uri: str) -> SqlAlchemyConversationStore:
    """A fresh conversation store on the per-test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest.
    :returns: A ready :class:`SqlAlchemyConversationStore`.
    """
    return SqlAlchemyConversationStore(db_uri)


@pytest.mark.asyncio
async def test_owner_gets_level_and_conversation(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """An owner is allowed and the fetched conversation is returned for reuse.

    The returned ``conversation`` is what lets the snapshot skip its own
    ``get_conversation`` read — assert it is the same session, not ``None``.
    """
    conv = conv_store.create_conversation()
    perm_store.ensure_user(ALICE)
    perm_store.grant(ALICE, conv.id, LEVEL_OWNER)

    access = await require_access_and_level(ALICE, conv.id, LEVEL_READ, perm_store, conv_store)

    assert access.level == LEVEL_OWNER, (
        f"owner must report level {LEVEL_OWNER}, got {access.level}"
    )
    assert access.conversation is not None, (
        "the conversation must be returned so the snapshot can reuse it"
    )
    assert access.conversation.id == conv.id


@pytest.mark.asyncio
async def test_no_access_raises_404_not_403(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """Bob, with no grant on Alice's session, gets 404 — not a 403 oracle.

    Returning 403 would confirm the session exists; 404 keeps existence
    hidden from a user with no access at all.
    """
    conv = conv_store.create_conversation()
    perm_store.ensure_user(ALICE)
    perm_store.ensure_user(BOB)
    perm_store.grant(ALICE, conv.id, LEVEL_OWNER)

    with pytest.raises(OmnigentError) as exc:
        await require_access_and_level(BOB, conv.id, LEVEL_READ, perm_store, conv_store)

    assert exc.value.code == ErrorCode.NOT_FOUND, f"no-access must be 404, got {exc.value.code}"


@pytest.mark.asyncio
async def test_insufficient_level_raises_403(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """A read-only user asking for edit gets 403 (has access, not enough)."""
    conv = conv_store.create_conversation()
    perm_store.ensure_user(BOB)
    perm_store.grant(BOB, conv.id, LEVEL_READ)

    with pytest.raises(OmnigentError) as exc:
        await require_access_and_level(BOB, conv.id, LEVEL_EDIT, perm_store, conv_store)

    assert exc.value.code == ErrorCode.FORBIDDEN, (
        f"insufficient level must be 403, got {exc.value.code}"
    )


@pytest.mark.asyncio
async def test_admin_allowed_and_bypasses_conversation_fetch(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """Admin is allowed at OWNER level and does not fetch the conversation.

    Mirrors ``check_session_access``'s admin short-circuit: ``conversation``
    is ``None`` (no lookup happened), and the level is ``LEVEL_OWNER``.
    """
    conv = conv_store.create_conversation()
    perm_store.ensure_user("root@test.com", is_admin=True)

    access = await require_access_and_level(
        "root@test.com", conv.id, LEVEL_OWNER, perm_store, conv_store
    )

    assert access.level == LEVEL_OWNER
    assert access.conversation is None, (
        "admin path must not fetch the conversation (it bypasses the lookup)"
    )


@pytest.mark.asyncio
async def test_public_grant_allows_but_level_reports_user_grant(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """Access via a higher public grant; displayed level is the user's own.

    The regression guard for the combined helper: a low user grant plus a
    higher ``__public__`` grant must still report the user's own level
    (matching ``get_permission_level``) while granting access via the
    public grant (matching ``check_access``).
    """
    conv = conv_store.create_conversation()
    perm_store.ensure_user(ALICE)
    perm_store.ensure_user(RESERVED_USER_PUBLIC)
    perm_store.grant(ALICE, conv.id, LEVEL_READ)  # user: read
    perm_store.grant(RESERVED_USER_PUBLIC, conv.id, LEVEL_OWNER)  # public: owner

    access = await require_access_and_level(ALICE, conv.id, LEVEL_EDIT, perm_store, conv_store)

    # Allowed (no raise) because the public grant satisfies EDIT ...
    assert access.conversation is not None
    assert access.conversation.id == conv.id, "must reuse the asked-for session"
    # ... but the displayed level is Alice's own read grant, unchanged.
    assert access.level == LEVEL_READ, (
        f"displayed level must be the user's own read grant, got {access.level}"
    )


@pytest.mark.asyncio
async def test_sub_agent_delegates_access_to_parent(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """A sub-agent session inherits access from its parent's grant.

    The user has a grant on the parent only; access to the sub-agent must
    be allowed via parent delegation, while the displayed level (a direct
    lookup on the sub-agent) stays ``None`` — unchanged from today.
    """
    parent = conv_store.create_conversation()
    child = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        sub_agent_name="summarizer",
    )
    perm_store.ensure_user(ALICE)
    perm_store.grant(ALICE, parent.id, LEVEL_OWNER)

    access = await require_access_and_level(ALICE, child.id, LEVEL_READ, perm_store, conv_store)

    assert access.conversation is not None
    assert access.conversation.id == child.id, "snapshot reuses the sub-agent row"
    # Displayed level is the direct grant on the sub-agent (none granted).
    assert access.level is None, "displayed level is the sub-agent's own grant, which is None here"


@pytest.mark.asyncio
async def test_permissions_disabled_returns_empty_access(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """With no permission store, the helper is a no-op (level None, no fetch)."""
    access = await require_access_and_level(
        None, "a42067bcc66e9b4bfaa3215131aefc96", LEVEL_READ, None, conv_store
    )

    assert access.level is None
    assert access.conversation is None


@pytest.mark.asyncio
async def test_unauthenticated_with_store_raises_401(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """An anonymous caller against an enabled store is rejected with 401."""
    conv = conv_store.create_conversation()

    with pytest.raises(OmnigentError) as exc:
        await require_access_and_level(None, conv.id, LEVEL_READ, perm_store, conv_store)

    assert exc.value.code == ErrorCode.UNAUTHORIZED


@pytest.mark.asyncio
async def test_missing_conversation_raises_404(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """A non-admin asking for a conversation that does not exist gets 404."""
    perm_store.ensure_user(ALICE)

    with pytest.raises(OmnigentError) as exc:
        await require_access_and_level(
            ALICE, "1d0b12236c77f69f5073a53583de1a3f", LEVEL_READ, perm_store, conv_store
        )

    assert exc.value.code == ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_access_check_shares_a_single_pool_checkout(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """The permission resolve + conversation + metadata reads share one checkout.

    On the per-streamed-event path this check re-runs for a session whose data
    is stable for the turn, so the pool checkout (plus ``pool_pre_ping``) is the
    cost that matters. The stores share one engine here, so the read burst must
    collapse to a single checkout rather than one per store call.
    """
    from sqlalchemy import event

    conv = conv_store.create_conversation()
    perm_store.ensure_user(ALICE)
    perm_store.grant(ALICE, conv.id, LEVEL_OWNER)

    engine = conv_store._engine
    checkouts: list[int] = []

    def _on_checkout(_dbapi: object, _record: object, _proxy: object) -> None:
        checkouts.append(1)

    event.listen(engine, "checkout", _on_checkout)
    try:
        access = await require_access_and_level(ALICE, conv.id, LEVEL_READ, perm_store, conv_store)
    finally:
        event.remove(engine, "checkout", _on_checkout)

    assert access.conversation is not None and access.conversation.id == conv.id
    assert len(checkouts) == 1, (
        f"the access-control read burst must share one checkout, got {len(checkouts)}"
    )


# ── require_agent_owner ────────────────────────────────────────────────────────


def test_agent_owner_allows_creator(
    perm_store: SqlAlchemyPermissionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded creator may mutate their session-scoped agent."""
    monkeypatch.setattr(_auth_helpers, "local_single_user_enabled", lambda: False)
    perm_store.ensure_user(ALICE)
    # No raise == allowed.
    require_agent_owner(ALICE, _session_agent(created_by=ALICE), perm_store)


def test_agent_owner_rejects_non_creator(
    perm_store: SqlAlchemyPermissionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different user (e.g. a shared editor) is refused with 403."""
    monkeypatch.setattr(_auth_helpers, "local_single_user_enabled", lambda: False)
    perm_store.ensure_user(BOB)
    with pytest.raises(OmnigentError) as exc:
        require_agent_owner(BOB, _session_agent(created_by=ALICE), perm_store)
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_agent_owner_admin_bypass(
    perm_store: SqlAlchemyPermissionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace admin may mutate an agent they do not own."""
    monkeypatch.setattr(_auth_helpers, "local_single_user_enabled", lambda: False)
    perm_store.ensure_user(BOB)
    perm_store.set_admin(BOB, True)
    require_agent_owner(BOB, _session_agent(created_by=ALICE), perm_store)


def test_agent_owner_disabled_auth_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no permission store (auth off), the check is a no-op."""
    monkeypatch.setattr(_auth_helpers, "local_single_user_enabled", lambda: False)
    require_agent_owner(None, _session_agent(created_by=ALICE), None)


def test_agent_owner_single_user_allows(
    perm_store: SqlAlchemyPermissionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local single-user mode short-circuits to allow (no second identity)."""
    monkeypatch.setattr(_auth_helpers, "local_single_user_enabled", lambda: True)
    require_agent_owner(None, _session_agent(created_by=None), perm_store)


def test_agent_owner_legacy_null_is_admin_only(
    perm_store: SqlAlchemyPermissionStore,
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy row (created_by=None) is admin-only — no session-owner fallback.

    The reverse lookup to an owning session is not dependable (reuse and session
    deletion), so an unowned agent can only be mutated by an admin; the original
    owner must re-upload to get a properly owned agent.
    """
    monkeypatch.setattr(_auth_helpers, "local_single_user_enabled", lambda: False)
    conv = conv_store.create_conversation()
    perm_store.ensure_user(ALICE)
    perm_store.grant(ALICE, conv.id, LEVEL_OWNER)

    agent = _session_agent(created_by=None, session_id=conv.id)
    # Even the owning session's owner is refused for a NULL row.
    with pytest.raises(OmnigentError) as exc:
        require_agent_owner(ALICE, agent, perm_store)
    assert exc.value.code == ErrorCode.FORBIDDEN

    # An admin may mutate it.
    perm_store.ensure_user(BOB)
    perm_store.set_admin(BOB, True)
    require_agent_owner(BOB, agent, perm_store)
