"""Tests for account entity dataclasses."""

from __future__ import annotations

from omnigent.entities.account import Account, AccountToken

# ── Account ───────────────────────────────────────────


def test_account_construction() -> None:
    acct = Account(
        id="alice@example.com",
        is_admin=True,
        created_at=1700000000,
        last_login_at=1700001000,
        has_password=True,
    )
    assert acct.id == "alice@example.com"
    assert acct.is_admin is True
    assert acct.created_at == 1700000000
    assert acct.last_login_at == 1700001000
    assert acct.has_password is True


def test_account_nullable_timestamps() -> None:
    """Legacy/header-auth rows have None timestamps."""
    acct = Account(
        id="local",
        is_admin=False,
        created_at=None,
        last_login_at=None,
        has_password=False,
    )
    assert acct.created_at is None
    assert acct.last_login_at is None


def test_account_is_frozen() -> None:
    """Account is a frozen dataclass — mutations raise."""
    acct = Account(
        id="bob",
        is_admin=False,
        created_at=1,
        last_login_at=None,
        has_password=False,
    )
    try:
        acct.id = "eve"  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")
    except AttributeError:
        pass


def test_account_equality() -> None:
    """Frozen dataclasses support value-based equality."""
    a = Account(id="x", is_admin=False, created_at=1, last_login_at=None, has_password=False)
    b = Account(id="x", is_admin=False, created_at=1, last_login_at=None, has_password=False)
    assert a == b


def test_account_inequality() -> None:
    a = Account(id="x", is_admin=False, created_at=1, last_login_at=None, has_password=False)
    b = Account(id="y", is_admin=False, created_at=1, last_login_at=None, has_password=False)
    assert a != b


# ── AccountToken ──────────────────────────────────────


def test_account_token_invite() -> None:
    token = AccountToken(
        id="tok_abc123",
        kind="invite",
        user_id=None,
        created_by="admin@example.com",
        created_at=1700000000,
        expires_at=1700086400,
        invited_is_admin=False,
    )
    assert token.kind == "invite"
    assert token.user_id is None
    assert token.created_by == "admin@example.com"
    assert token.invited_is_admin is False


def test_account_token_magic() -> None:
    token = AccountToken(
        id="tok_magic_xyz",
        kind="magic",
        user_id="alice@example.com",
        created_by=None,
        created_at=1700000000,
        expires_at=1700003600,
        invited_is_admin=False,
    )
    assert token.kind == "magic"
    assert token.user_id == "alice@example.com"
    assert token.created_by is None


def test_account_token_is_frozen() -> None:
    token = AccountToken(
        id="tok_1",
        kind="invite",
        user_id=None,
        created_by="admin",
        created_at=1,
        expires_at=2,
        invited_is_admin=True,
    )
    try:
        token.id = "tok_2"  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")
    except AttributeError:
        pass
