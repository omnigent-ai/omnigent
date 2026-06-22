"""Tests for permission entity dataclasses."""

from __future__ import annotations

from omnigent.entities.permission import ResolvedAccess, SessionPermission

# ── SessionPermission ─────────────────────────────────


def test_session_permission_construction() -> None:
    perm = SessionPermission(
        user_id="alice@example.com",
        conversation_id="conv_abc123",
        level=2,
    )
    assert perm.user_id == "alice@example.com"
    assert perm.conversation_id == "conv_abc123"
    assert perm.level == 2


def test_session_permission_public() -> None:
    """Public access uses the __public__ sentinel."""
    perm = SessionPermission(
        user_id="__public__",
        conversation_id="conv_1",
        level=1,
    )
    assert perm.user_id == "__public__"
    assert perm.level == 1


def test_session_permission_is_mutable() -> None:
    perm = SessionPermission(user_id="u", conversation_id="c", level=1)
    perm.level = 3
    assert perm.level == 3


# ── ResolvedAccess ────────────────────────────────────


def test_resolved_access_admin() -> None:
    access = ResolvedAccess(
        is_admin=True,
        user_grant_level=None,
        public_grant_level=None,
    )
    assert access.is_admin is True
    assert access.user_grant_level is None
    assert access.public_grant_level is None


def test_resolved_access_user_grant() -> None:
    access = ResolvedAccess(
        is_admin=False,
        user_grant_level=2,
        public_grant_level=None,
    )
    assert access.user_grant_level == 2


def test_resolved_access_public_grant() -> None:
    access = ResolvedAccess(
        is_admin=False,
        user_grant_level=None,
        public_grant_level=1,
    )
    assert access.public_grant_level == 1


def test_resolved_access_both_grants() -> None:
    """User may have both a direct grant and public access."""
    access = ResolvedAccess(
        is_admin=False,
        user_grant_level=3,
        public_grant_level=1,
    )
    assert access.user_grant_level == 3
    assert access.public_grant_level == 1


def test_resolved_access_is_frozen() -> None:
    access = ResolvedAccess(is_admin=False, user_grant_level=None, public_grant_level=None)
    try:
        access.is_admin = True  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")
    except AttributeError:
        pass


def test_resolved_access_equality() -> None:
    a = ResolvedAccess(is_admin=True, user_grant_level=3, public_grant_level=1)
    b = ResolvedAccess(is_admin=True, user_grant_level=3, public_grant_level=1)
    assert a == b


def test_resolved_access_inequality() -> None:
    a = ResolvedAccess(is_admin=True, user_grant_level=3, public_grant_level=1)
    b = ResolvedAccess(is_admin=False, user_grant_level=3, public_grant_level=1)
    assert a != b
