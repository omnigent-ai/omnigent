"""Unit tests for the session permission resolution function.

Tests :func:`omnigent.server.permissions.check_session_access` and
:func:`omnigent.server.permissions.check_is_manager` against all
resolution branches:

1. Admin user -> allow
2. Sub-agent -> delegate to parent conversation
3. Unauthenticated (user_id=None) -> deny
4. Direct grant with sufficient level -> allow
5. Direct grant with insufficient level -> deny
6. __public__ sentinel grant -> allow for read, deny for edit
7. No grant at all -> deny
8. Conversation not found -> deny
9. Nested sub-agent (grandchild) -> delegates up the chain

Uses in-memory store stubs (no database, no HTTP). Real
:class:`SessionPermission` and :class:`Conversation` dataclass
instances are used for all data objects so isinstance checks and
attribute access behave identically to production.
"""

from __future__ import annotations

import pytest

from omnigent.entities.conversation import Conversation
from omnigent.entities.permission import ResolvedAccess, SessionPermission
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    env_var_is_truthy,
)
from omnigent.server.permissions import (
    check_is_manager,
    check_session_access,
    resolved_allows,
    resolved_level,
)

# ── In-memory store stubs ────────────────────────────────────────
#
# Real stub classes instead of MagicMock.  Each raises
# AssertionError on methods not expected during permission checks
# so we fail loud if the production code starts calling something
# unexpected.


class _StubPermissionStore:
    """In-memory permission store for permission-check tests.

    Holds a dict of ``(user_id, conversation_id) -> SessionPermission``
    and a set of admin user IDs.  Only ``get`` and ``is_admin`` are
    called by :func:`check_session_access`; every other method raises
    so tests break loudly if the production code reaches them.
    """

    def __init__(self) -> None:
        """Initialize empty grant table and admin set."""
        self._grants: dict[tuple[str, str], SessionPermission] = {}
        self._admins: set[str] = set()

    # -- methods used by check_session_access --

    def get(self, user_id: str, conversation_id: str) -> SessionPermission | None:
        """Look up a single grant by ``(user_id, conversation_id)``."""
        return self._grants.get((user_id, conversation_id))

    def is_admin(self, user_id: str) -> bool:
        """Return whether ``user_id`` is in the admin set."""
        return user_id in self._admins

    # -- helpers for test setup --

    def add_grant(self, user_id: str, conversation_id: str, level: int) -> None:
        """Insert a grant into the in-memory table."""
        self._grants[(user_id, conversation_id)] = SessionPermission(
            user_id=user_id,
            conversation_id=conversation_id,
            level=level,
        )

    def add_admin(self, user_id: str) -> None:
        """Mark a user as admin."""
        self._admins.add(user_id)

    # -- methods delegated to by check_session_access / route helpers --

    def check_access(self, user_id: str | None, conversation_id: str, required_level: int) -> bool:
        """Grant-level access check (no admin, no parent delegation)."""
        if user_id is None:
            return False
        grant = self.get(user_id, conversation_id)
        if grant is not None and grant.level >= required_level:
            return True
        public_grant = self.get(RESERVED_USER_PUBLIC, conversation_id)
        if public_grant is not None and public_grant.level >= required_level:
            return True
        return False

    def get_permission_level(self, user_id: str | None, conversation_id: str) -> int | None:
        """Return effective permission level for UI display."""
        if user_id is None:
            return None
        if self.is_admin(user_id):
            return LEVEL_OWNER
        grant = self.get(user_id, conversation_id)
        if grant is not None:
            return grant.level
        public_grant = self.get(RESERVED_USER_PUBLIC, conversation_id)
        if public_grant is not None:
            return public_grant.level
        return None


class _StubConversationStore:
    """In-memory conversation store for permission-check tests.

    Holds a dict of ``conversation_id -> Conversation``.  Only
    ``get_conversation`` is called by :func:`check_session_access`; all
    other methods raise so tests break loudly.
    """

    def __init__(self) -> None:
        """Initialize empty conversation table."""
        self._conversations: dict[str, Conversation] = {}

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Look up a conversation by ID."""
        return self._conversations.get(conversation_id)

    def add(self, conv: Conversation) -> None:
        """Insert a conversation into the in-memory table."""
        self._conversations[conv.id] = conv


def _make_conversation(
    conv_id: str,
    *,
    kind: str = "default",
    parent_conversation_id: str | None = None,
) -> Conversation:
    """Build a minimal Conversation dataclass for tests.

    Timestamps are fixed at 0 — irrelevant for permission logic.
    """
    return Conversation(
        id=conv_id,
        created_at=0,
        updated_at=0,
        root_conversation_id=parent_conversation_id or conv_id,
        kind=kind,
        parent_conversation_id=parent_conversation_id,
    )


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture()
def perm_store() -> _StubPermissionStore:
    """Fresh in-memory permission store."""
    return _StubPermissionStore()


@pytest.fixture()
def conv_store() -> _StubConversationStore:
    """Fresh in-memory conversation store."""
    return _StubConversationStore()


# ── 1. Admin user -> allow ───────────────────────────────────────


def test_admin_user_allowed_for_any_level(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """An admin user is granted access at any required level.

    The admin check is the very first branch in the resolution
    algorithm — it does not even look up the conversation or any
    grants.  We deliberately do NOT add the conversation to
    conv_store to prove that the admin short-circuit fires before
    the conversation lookup.
    """
    perm_store.add_admin("admin@example.com")

    for level in (LEVEL_READ, LEVEL_EDIT, LEVEL_MANAGE):
        # Admin bypasses all other checks -- conv_store is empty and
        # that is fine because the admin branch returns before the
        # conversation lookup.
        result = check_session_access(
            user_id="admin@example.com",
            conversation_id="conv_does_not_exist",
            required_level=level,
            permission_store=perm_store,  # type: ignore[arg-type]
            conversation_store=conv_store,  # type: ignore[arg-type]
        )
        # Admin user bypasses all grant checks.  If False, the
        # is_admin() call isn't wired correctly or the admin
        # short-circuit branch was removed.
        assert result is True, (
            f"Admin should be allowed at level={level}. "
            f"If False, the admin short-circuit in check_session_access "
            f"is not firing."
        )


# ── 2. Sub-agent -> delegate to parent ───────────────────────────


def test_sub_agent_delegates_to_parent(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """A sub-agent conversation delegates permission checks to its parent.

    The user has a read grant on the parent but not on the child.
    Access should still be allowed because the child's
    ``parent_conversation_id`` triggers recursive delegation.
    """
    parent = _make_conversation("conv_parent")
    child = _make_conversation(
        "conv_child",
        kind="sub_agent",
        parent_conversation_id="conv_parent",
    )
    conv_store.add(parent)
    conv_store.add(child)
    perm_store.add_grant("alice@example.com", "conv_parent", LEVEL_READ)

    # The child conversation has no direct grant for alice, but its
    # parent does.  Delegation should resolve to the parent's grant.
    result = check_session_access(
        user_id="alice@example.com",
        conversation_id="conv_child",
        required_level=LEVEL_READ,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # Sub-agent should delegate to parent.  If False, the
    # parent_conversation_id recursion is broken.
    assert result is True, (
        "Sub-agent conversation should delegate to parent's grants. "
        "If False, the recursive delegation via parent_conversation_id "
        "is not working."
    )


# ── 3. Unauthenticated (user_id=None) -> deny ───────────────────


def test_unauthenticated_user_denied(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """An unauthenticated request (user_id=None) is always denied.

    Even if a __public__ grant exists for read, unauthenticated users
    are denied before reaching the public-grant fallback.
    """
    conv = _make_conversation("conv_public")
    conv_store.add(conv)
    perm_store.add_grant(RESERVED_USER_PUBLIC, "conv_public", LEVEL_READ)

    result = check_session_access(
        user_id=None,
        conversation_id="conv_public",
        required_level=LEVEL_READ,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # user_id=None is denied before any grant lookup.  If True, the
    # None guard is missing or placed after the grant checks.
    assert result is False, (
        "Unauthenticated (user_id=None) should always be denied. "
        "If True, the None check is missing or after grant lookups."
    )


# ── 4. Direct grant with sufficient level -> allow ───────────────


@pytest.mark.parametrize(
    "grant_level,required_level",
    [
        (LEVEL_READ, LEVEL_READ),
        (LEVEL_EDIT, LEVEL_READ),
        (LEVEL_EDIT, LEVEL_EDIT),
        (LEVEL_MANAGE, LEVEL_READ),
        (LEVEL_MANAGE, LEVEL_EDIT),
        (LEVEL_MANAGE, LEVEL_MANAGE),
    ],
    ids=[
        "read_grants_read",
        "edit_grants_read",
        "edit_grants_edit",
        "manage_grants_read",
        "manage_grants_edit",
        "manage_grants_manage",
    ],
)
def test_direct_grant_sufficient_level(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
    grant_level: int,
    required_level: int,
) -> None:
    """A direct grant whose level >= required level allows access.

    Parametrized over all valid (grant_level, required_level) pairs
    where grant_level >= required_level.
    """
    conv = _make_conversation("conv_1")
    conv_store.add(conv)
    perm_store.add_grant("bob@example.com", "conv_1", grant_level)

    result = check_session_access(
        user_id="bob@example.com",
        conversation_id="conv_1",
        required_level=required_level,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # Grant level is >= required level, so access must be allowed.
    # If False, the level comparison is wrong (e.g. using == instead
    # of >=) or the grant lookup is broken.
    assert result is True, (
        f"grant_level={grant_level} >= required_level={required_level} "
        f"should allow access. If False, the >= comparison or "
        f"grant lookup is broken."
    )


# ── 5. Direct grant with insufficient level -> deny ──────────────


@pytest.mark.parametrize(
    "grant_level,required_level",
    [
        (LEVEL_READ, LEVEL_EDIT),
        (LEVEL_READ, LEVEL_MANAGE),
        (LEVEL_EDIT, LEVEL_MANAGE),
    ],
    ids=[
        "read_denies_edit",
        "read_denies_manage",
        "edit_denies_manage",
    ],
)
def test_direct_grant_insufficient_level(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
    grant_level: int,
    required_level: int,
) -> None:
    """A direct grant whose level < required level denies access.

    Parametrized over all (grant_level, required_level) pairs where
    grant_level < required_level and no __public__ fallback exists.
    """
    conv = _make_conversation("conv_1")
    conv_store.add(conv)
    perm_store.add_grant("bob@example.com", "conv_1", grant_level)

    result = check_session_access(
        user_id="bob@example.com",
        conversation_id="conv_1",
        required_level=required_level,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # Grant level < required level — access must be denied.  If True,
    # the level comparison is using the wrong operator (e.g. > instead
    # of >=, or checking the wrong direction).
    assert result is False, (
        f"grant_level={grant_level} < required_level={required_level} "
        f"should deny access. If True, the level comparison is inverted."
    )


# ── 6. __public__ sentinel grant ─────────────────────────────────


def test_public_grant_allows_read(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """The __public__ sentinel allows an authenticated user with no
    direct grant to read a session that has a public read grant.
    """
    conv = _make_conversation("conv_shared")
    conv_store.add(conv)
    # No direct grant for carol -- only a __public__ read grant.
    perm_store.add_grant(RESERVED_USER_PUBLIC, "conv_shared", LEVEL_READ)

    result = check_session_access(
        user_id="carol@example.com",
        conversation_id="conv_shared",
        required_level=LEVEL_READ,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # __public__ read grant should cover authenticated users who lack
    # a direct grant.  If False, the public fallback branch is broken.
    assert result is True, (
        "An authenticated user with no direct grant should be allowed "
        "to read via the __public__ sentinel. If False, the public "
        "fallback branch is not reached or not working."
    )


def test_public_grant_denies_edit(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """The __public__ sentinel with read level does not allow edit.

    Even though a __public__ grant exists, its level (read=1) is
    below the required level (edit=2), so access is denied.
    """
    conv = _make_conversation("conv_shared")
    conv_store.add(conv)
    perm_store.add_grant(RESERVED_USER_PUBLIC, "conv_shared", LEVEL_READ)

    result = check_session_access(
        user_id="carol@example.com",
        conversation_id="conv_shared",
        required_level=LEVEL_EDIT,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # __public__ read grant is insufficient for edit access.  If True,
    # the public grant's level is not being compared correctly.
    assert result is False, (
        "A __public__ read grant should not allow edit-level access. "
        "If True, the public grant level comparison is wrong."
    )


# ── 7. No grant at all -> deny ───────────────────────────────────


def test_no_grant_denies_access(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """An authenticated user with no grant at all is denied access.

    Neither a direct grant nor a __public__ grant exists for the
    session.
    """
    conv = _make_conversation("conv_private")
    conv_store.add(conv)

    result = check_session_access(
        user_id="eve@example.com",
        conversation_id="conv_private",
        required_level=LEVEL_READ,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # No grants exist at all — must be denied.  If True, there is
    # a fallback path that defaults to allowing access.
    assert result is False, (
        "A user with no direct or public grant should be denied. "
        "If True, there is an unwanted default-allow path."
    )


# ── 8. Conversation not found -> deny ────────────────────────────


def test_conversation_not_found_denies_access(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """A non-existent conversation always results in denial.

    Even if the user has admin-like grants elsewhere, a missing
    conversation returns False because the lookup precedes the
    grant checks.
    """
    # conv_store is empty — no conversations at all.
    perm_store.add_grant("alice@example.com", "conv_gone", LEVEL_MANAGE)

    result = check_session_access(
        user_id="alice@example.com",
        conversation_id="conv_gone",
        required_level=LEVEL_READ,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # Conversation not found -> deny before any grant checks.
    # If True, the conversation lookup is being skipped or the
    # function doesn't check for None.
    assert result is False, (
        "A missing conversation should deny access. If True, the "
        "conversation_store.get_conversation() None-check is missing."
    )


# ── 9. Nested sub-agent (grandchild) -> delegates up the chain ───


def test_nested_sub_agent_delegates_to_root(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """A grandchild sub-agent delegates permission up to the root.

    Chain: grandchild -> child -> root.
    The user only has a grant on the root conversation, but
    access should be allowed at the grandchild level via
    double delegation.
    """
    root = _make_conversation("conv_root")
    child = _make_conversation(
        "conv_child",
        kind="sub_agent",
        parent_conversation_id="conv_root",
    )
    grandchild = _make_conversation(
        "conv_grandchild",
        kind="sub_agent",
        parent_conversation_id="conv_child",
    )
    conv_store.add(root)
    conv_store.add(child)
    conv_store.add(grandchild)
    perm_store.add_grant("alice@example.com", "conv_root", LEVEL_EDIT)

    # Access the grandchild — should delegate to child, then to root.
    result = check_session_access(
        user_id="alice@example.com",
        conversation_id="conv_grandchild",
        required_level=LEVEL_READ,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # Grandchild -> child -> root.  Root has an edit grant which
    # covers read.  If False, the recursion only goes one level deep.
    assert result is True, (
        "Grandchild should delegate through child to root. "
        "If False, the recursive delegation does not traverse "
        "multiple parent levels."
    )


def test_nested_sub_agent_denied_when_root_insufficient(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """A grandchild sub-agent is denied when the root grant is insufficient.

    Chain: grandchild -> child -> root.
    The user only has a read grant on root but needs manage level.
    """
    root = _make_conversation("conv_root")
    child = _make_conversation(
        "conv_child",
        kind="sub_agent",
        parent_conversation_id="conv_root",
    )
    grandchild = _make_conversation(
        "conv_grandchild",
        kind="sub_agent",
        parent_conversation_id="conv_child",
    )
    conv_store.add(root)
    conv_store.add(child)
    conv_store.add(grandchild)
    perm_store.add_grant("alice@example.com", "conv_root", LEVEL_READ)

    result = check_session_access(
        user_id="alice@example.com",
        conversation_id="conv_grandchild",
        required_level=LEVEL_MANAGE,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # Delegation reaches root, but root only has read — manage is
    # denied.  If True, the level check is skipped during recursion.
    assert result is False, (
        "Grandchild delegates to root, but root only has read-level "
        "grant — manage should be denied. If True, level comparison "
        "is broken in the recursive path."
    )


# ── check_is_manager shorthand ───────────────────────────────────


def test_check_is_manager_delegates_correctly(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """check_is_manager is a thin wrapper that checks for LEVEL_MANAGE.

    Verifies both the allowed and denied cases.
    """
    conv = _make_conversation("conv_mgr")
    conv_store.add(conv)
    perm_store.add_grant("manager@example.com", "conv_mgr", LEVEL_MANAGE)
    perm_store.add_grant("editor@example.com", "conv_mgr", LEVEL_EDIT)

    # Manager-level grant should pass.
    assert (
        check_is_manager(
            user_id="manager@example.com",
            conversation_id="conv_mgr",
            permission_store=perm_store,  # type: ignore[arg-type]
            conversation_store=conv_store,  # type: ignore[arg-type]
        )
        is True
    ), (
        "User with LEVEL_MANAGE should be recognized as manager. "
        "If False, check_is_manager is not passing LEVEL_MANAGE."
    )

    # Editor-level grant should not pass for manage.
    assert (
        check_is_manager(
            user_id="editor@example.com",
            conversation_id="conv_mgr",
            permission_store=perm_store,  # type: ignore[arg-type]
            conversation_store=conv_store,  # type: ignore[arg-type]
        )
        is False
    ), (
        "User with LEVEL_EDIT should not pass check_is_manager. "
        "If True, check_is_manager is using a lower level than MANAGE."
    )


# ── Edge: admin bypasses conversation-not-found ──────────────────


def test_admin_bypasses_missing_conversation(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """Admin is allowed even when the conversation does not exist.

    The admin check fires before the conversation lookup, so a
    missing conversation does not block an admin.
    """
    perm_store.add_admin("admin@example.com")
    # conv_store is intentionally empty.

    result = check_session_access(
        user_id="admin@example.com",
        conversation_id="conv_nonexistent",
        required_level=LEVEL_MANAGE,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # Admin short-circuits before conversation lookup.  If False,
    # the admin check was moved after the conversation lookup.
    assert result is True, (
        "Admin should be allowed even when the conversation does not "
        "exist. If False, the admin check was moved after the "
        "conversation lookup."
    )


# ── Edge: direct grant takes precedence over __public__ ──────────


def test_direct_grant_takes_precedence_over_public(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """A direct grant is checked before the __public__ fallback.

    The user has a manage-level direct grant. There is also a
    read-level public grant. The manage-level direct grant should
    take precedence.
    """
    conv = _make_conversation("conv_both")
    conv_store.add(conv)
    perm_store.add_grant("alice@example.com", "conv_both", LEVEL_MANAGE)
    perm_store.add_grant(RESERVED_USER_PUBLIC, "conv_both", LEVEL_READ)

    # Manage-level required — direct grant covers it; public would not.
    result = check_session_access(
        user_id="alice@example.com",
        conversation_id="conv_both",
        required_level=LEVEL_MANAGE,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # Direct manage-level grant satisfies manage requirement.  If
    # False, the direct grant lookup is skipped and only the public
    # fallback (read) is checked.
    assert result is True, (
        "Direct manage grant should satisfy manage requirement even "
        "though public grant is only read. If False, the direct "
        "grant lookup is broken or skipped."
    )


# ── Edge: non-admin, non-sub-agent, user_id=None ────────────────


def test_unauthenticated_on_sub_agent_still_denied(
    perm_store: _StubPermissionStore,
    conv_store: _StubConversationStore,
) -> None:
    """Unauthenticated access to a sub-agent conversation is denied.

    Even though the sub-agent delegates to its parent, the
    user_id=None check on the parent conversation denies access.
    """
    parent = _make_conversation("conv_parent")
    child = _make_conversation(
        "conv_child",
        kind="sub_agent",
        parent_conversation_id="conv_parent",
    )
    conv_store.add(parent)
    conv_store.add(child)
    # Public read grant on parent — but user_id=None is blocked first.
    perm_store.add_grant(RESERVED_USER_PUBLIC, "conv_parent", LEVEL_READ)

    result = check_session_access(
        user_id=None,
        conversation_id="conv_child",
        required_level=LEVEL_READ,
        permission_store=perm_store,  # type: ignore[arg-type]
        conversation_store=conv_store,  # type: ignore[arg-type]
    )
    # user_id=None is denied even after delegation to parent.  If
    # True, the None guard is missing from the recursive path.
    assert result is False, (
        "Unauthenticated user accessing sub-agent should be denied "
        "after delegation to parent. If True, the user_id=None "
        "guard is missing in the recursive call."
    )


# ── Header mode: missing header -> None (401) ────────────────


@pytest.mark.parametrize("raw_value", ["1", "true", "TRUE", "yes", " Yes "])
def test_env_var_is_truthy_accepts_existing_truthy_values(
    raw_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boolean env parsing matches existing harness truthy semantics."""
    monkeypatch.setenv("TEST_TRUTHY_VAR", raw_value)

    assert env_var_is_truthy("TEST_TRUTHY_VAR") is True


@pytest.mark.parametrize("raw_value", ["", "0", "false", "on", "no"])
def test_env_var_is_truthy_rejects_non_truthy_values(
    raw_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ``1``, ``true``, and ``yes`` are truthy."""
    monkeypatch.setenv("TEST_TRUTHY_VAR", raw_value)

    assert env_var_is_truthy("TEST_TRUTHY_VAR") is False


def test_env_var_is_truthy_uses_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset or empty env vars return the caller-provided default."""
    monkeypatch.delenv("TEST_TRUTHY_VAR", raising=False)

    assert env_var_is_truthy("TEST_TRUTHY_VAR") is False
    assert env_var_is_truthy("TEST_TRUTHY_VAR", default=True) is True


def test_header_mode_rejects_missing_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UnifiedAuthProvider(source="header") returns ``None`` (→ 401)
    when X-Forwarded-Email is absent.

    A missing or proxy-dropped header must fail closed.
    Falling back to the shared ``"local"`` identity here would give
    every unauthenticated request OWNER access to every other
    unauthenticated user's sessions. The fallback is reserved for
    explicit single-user local runtimes (OMNIGENT_LOCAL_SINGLE_USER=1).
    """
    from unittest.mock import MagicMock

    from omnigent.server.auth import UnifiedAuthProvider

    # Clear the single-user marker so an ambient value from the dev
    # shell can't flip the provider into the fallback path.
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
    provider = UnifiedAuthProvider(source="header")
    # Build a minimal mock request with no headers.
    # MagicMock is acceptable here: we only need
    # request.headers.get("X-Forwarded-Email") to return None,
    # and Request is a complex ASGI object that cannot be trivially
    # constructed without a real scope.
    mock_request = MagicMock()
    mock_request.headers = {}

    result = provider.get_user_id(mock_request)

    assert result is None, (
        f"Expected None (fail closed → 401) for missing header, got {result!r}. "
        f"A non-None value means unauthenticated requests resolve to a shared "
        f"identity (regression)."
    )


def test_header_mode_accepts_valid_header() -> None:
    """UnifiedAuthProvider(source="header") returns the user_id from
    a valid header.

    This is the positive counterpart to the missing-header test: a
    valid, non-reserved header value is accepted.
    """
    from unittest.mock import MagicMock

    from omnigent.server.auth import UnifiedAuthProvider

    provider = UnifiedAuthProvider(source="header")
    mock_request = MagicMock()
    mock_request.headers = {"X-Forwarded-Email": "alice@example.com"}

    result = provider.get_user_id(mock_request)

    assert result == "alice@example.com", f"Expected 'alice@example.com', got {result!r}."


# ── Reserved names in header mode ────────────────────────────


@pytest.mark.parametrize(
    "reserved_name",
    ["local", "__public__"],
    ids=["local", "public"],
)
def test_header_mode_rejects_reserved_names(reserved_name: str) -> None:
    """UnifiedAuthProvider(source="header") rejects reserved usernames.

    The reserved names "local" and "__public__" must not be usable as
    real user identities. If accepted, a client could impersonate
    the single-user admin account or the public sentinel, bypassing
    permission checks.
    """
    from unittest.mock import MagicMock

    from omnigent.server.auth import UnifiedAuthProvider

    provider = UnifiedAuthProvider(source="header")
    mock_request = MagicMock()
    mock_request.headers = {"X-Forwarded-Email": reserved_name}

    result = provider.get_user_id(mock_request)

    assert result is None, (
        f"Reserved name {reserved_name!r} should be rejected (return None), but got {result!r}."
    )


# ── resolved_allows / resolved_level (in-memory policy) ──────────────


def test_resolved_allows_admin_bypasses_everything() -> None:
    """Admin is allowed at any level with no grants — mirrors the bypass."""
    access = ResolvedAccess(is_admin=True, user_grant_level=None, public_grant_level=None)
    assert resolved_allows(access, LEVEL_OWNER) is True
    assert resolved_level(access) == LEVEL_OWNER


def test_resolved_allows_user_grant_sufficient() -> None:
    """A user grant >= required allows; below required denies."""
    access = ResolvedAccess(is_admin=False, user_grant_level=LEVEL_EDIT, public_grant_level=None)
    assert resolved_allows(access, LEVEL_EDIT) is True
    assert resolved_allows(access, LEVEL_MANAGE) is False


def test_resolved_allows_public_grant_satisfies_access() -> None:
    """A sufficient ``__public__`` grant allows even with no user grant."""
    access = ResolvedAccess(is_admin=False, user_grant_level=None, public_grant_level=LEVEL_READ)
    assert resolved_allows(access, LEVEL_READ) is True
    assert resolved_allows(access, LEVEL_EDIT) is False


def test_resolved_level_prefers_user_grant_over_public() -> None:
    """The displayed level is the user's own grant, NOT a higher public one.

    This is the asymmetry between access and displayed level: with a low
    user grant and a higher public grant, ``resolved_allows`` is satisfied
    by the public grant, but ``resolved_level`` reports the user's own
    grant — exactly matching ``get_permission_level`` so the combined
    helper does not change the displayed level.
    """
    access = ResolvedAccess(
        is_admin=False, user_grant_level=LEVEL_READ, public_grant_level=LEVEL_MANAGE
    )
    # Access at EDIT is granted via the public (manage) grant ...
    assert resolved_allows(access, LEVEL_EDIT) is True
    # ... but the level shown to the UI is the user's own read grant.
    assert resolved_level(access) == LEVEL_READ


def test_resolved_level_falls_back_to_public_when_no_user_grant() -> None:
    """With no user grant, the displayed level falls back to the public grant."""
    access = ResolvedAccess(is_admin=False, user_grant_level=None, public_grant_level=LEVEL_READ)
    assert resolved_level(access) == LEVEL_READ


def test_resolved_level_none_when_no_access() -> None:
    """No admin, no grants → no displayed level."""
    access = ResolvedAccess(is_admin=False, user_grant_level=None, public_grant_level=None)
    assert resolved_level(access) is None
    assert resolved_allows(access, LEVEL_READ) is False
