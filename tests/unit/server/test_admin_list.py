"""Tests for the file-backed admin roster (:mod:`omnigent.server.admin_list`).

Covers three layers:

1. The mtime-cached file loader (``MtimeCachedIdentitySet`` / ``AdminList``)
   — parsing, comments, lowercasing, missing/unreadable files, and the
   reload-on-mtime-change behavior that lets an operator edit the file
   without restarting the server.
2. Path resolution (``resolve_data_dir`` / ``resolve_admin_list_path``)
   — the env override and the credentials-dir co-location default.
3. ``promote_if_listed`` against the two real stores it serves
   (``SqlAlchemyPermissionStore`` for OIDC, ``SqlAlchemyAccountStore``
   for accounts) plus an end-to-end check through the accounts login
   route, including the **additive** invariant: removing an identity
   from the file must NOT demote an already-admin user.

Real types throughout (real stores, real SQLite via ``db_uri``), and
assertions check the actual ``is_admin`` value that proves promotion
flowed through, not just that a call returned.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.admin_list import (
    AdminList,
    MtimeCachedIdentitySet,
    load_admin_list,
    promote_if_listed,
    resolve_admin_list_path,
    resolve_data_dir,
)
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.passwords import hash_password
from omnigent.server.routes.accounts_auth import create_accounts_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

# ── File loader: parsing ──────────────────────────────────────────


def test_loader_parses_one_identity_per_line(tmp_path: Path) -> None:
    """Each non-blank line becomes one set member.

    The whole feature rests on this: if parsing breaks, no one is ever
    promoted (or everyone matches an empty string).
    """
    f = tmp_path / "admins"
    f.write_text("alice@example.com\nbob@example.com\n")
    s = MtimeCachedIdentitySet(f)
    assert s.snapshot() == frozenset({"alice@example.com", "bob@example.com"})


def test_loader_lowercases_and_strips(tmp_path: Path) -> None:
    """Identities are normalized (lowercase, whitespace-trimmed).

    OIDC emails arrive lowercased in the callback, so the file must
    match case-insensitively or a capitalized entry would silently
    never match.
    """
    f = tmp_path / "admins"
    f.write_text("  Alice@Example.COM  \n")
    s = MtimeCachedIdentitySet(f)
    assert s.contains("alice@example.com")
    assert s.contains("ALICE@example.com")  # query is also normalized


def test_loader_ignores_comments_and_blanks(tmp_path: Path) -> None:
    """``#`` comments (inline + whole-line) and blank lines are dropped.

    Without this, a ``# comment`` line would be treated as an admin
    identity ``"# comment"`` — harmless but wrong, and an inline
    ``alice  # founder`` would store the comment as part of the id and
    never match.
    """
    f = tmp_path / "admins"
    f.write_text("# Omnigent admins\n\nalice@example.com   # founder\n   \nbob@example.com\n")
    s = MtimeCachedIdentitySet(f)
    assert s.snapshot() == frozenset({"alice@example.com", "bob@example.com"})


def test_loader_missing_file_is_empty(tmp_path: Path) -> None:
    """An absent file yields an empty set, not an error.

    The admin list is optional; an auth path must never fail because
    the operator hasn't created the file.
    """
    s = MtimeCachedIdentitySet(tmp_path / "does-not-exist")
    assert s.snapshot() == frozenset()
    assert not s.contains("alice@example.com")


def test_loader_picks_up_file_created_after_construction(tmp_path: Path) -> None:
    """A file created after the set is constructed is read on next access.

    The provider builds the AdminList at startup; the operator may
    create the file later. The absent-file branch is re-checked every
    call, so creation takes effect immediately.
    """
    f = tmp_path / "admins"
    s = MtimeCachedIdentitySet(f)
    assert not s.contains("alice@example.com")
    f.write_text("alice@example.com\n")
    assert s.contains("alice@example.com")


def test_loader_reloads_on_mtime_change(tmp_path: Path) -> None:
    """Editing the file (new mtime) reloads the set — no restart needed.

    This is the whole point of the mtime cache: operator edits take
    effect on the next login. We force a distinct mtime with os.utime
    (rather than sleeping) so the test is deterministic.
    """
    f = tmp_path / "admins"
    f.write_text("alice@example.com\n")
    s = MtimeCachedIdentitySet(f)
    assert s.snapshot() == frozenset({"alice@example.com"})

    f.write_text("alice@example.com\nbob@example.com\n")
    # Force a strictly-later mtime so the change is observable even if
    # both writes land within the same filesystem timestamp tick.
    stat = f.stat()
    os.utime(f, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert s.snapshot() == frozenset({"alice@example.com", "bob@example.com"})


def test_loader_unreadable_file_is_empty(tmp_path: Path) -> None:
    """A present-but-unreadable file fails open to empty (login still works).

    Skipped when running as root (chmod 000 is bypassed by root, so
    the unreadable branch can't be exercised).
    """
    if os.geteuid() == 0:
        pytest.skip("cannot exercise unreadable-file branch as root")
    f = tmp_path / "admins"
    f.write_text("alice@example.com\n")
    f.chmod(0o000)
    try:
        s = MtimeCachedIdentitySet(f)
        assert s.snapshot() == frozenset()
    finally:
        f.chmod(0o600)  # let tmp cleanup remove it


# ── Path resolution ───────────────────────────────────────────────


def test_resolve_admin_list_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OMNIGENT_ADMIN_LIST_PATH`` wins over the default."""
    monkeypatch.setenv("OMNIGENT_ADMIN_LIST_PATH", "/etc/omnigent/admins")
    assert resolve_admin_list_path() == Path("/etc/omnigent/admins")


def test_resolve_data_dir_uses_credentials_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Data dir co-locates with the credentials file (Docker ``/data``)."""
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", "/data/admin-credentials")
    monkeypatch.delenv("OMNIGENT_ADMIN_LIST_PATH", raising=False)
    assert resolve_data_dir() == Path("/data")
    assert resolve_admin_list_path() == Path("/data/admins")


def test_resolve_data_dir_defaults_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env, the data dir is ``~/.omnigent``."""
    monkeypatch.delenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", raising=False)
    monkeypatch.delenv("OMNIGENT_ADMIN_LIST_PATH", raising=False)
    assert resolve_data_dir() == Path.home() / ".omnigent"
    assert resolve_admin_list_path() == Path.home() / ".omnigent" / "admins"


def test_load_admin_list_binds_resolved_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_admin_list`` constructs an AdminList at the resolved path."""
    monkeypatch.setenv("OMNIGENT_ADMIN_LIST_PATH", "/tmp/omnigent-admins-test")
    al = load_admin_list()
    assert al.path == Path("/tmp/omnigent-admins-test")


# ── config admins (the `extra` set) + file union ──────────────────


def test_admin_list_extra_from_config(tmp_path: Path) -> None:
    """A config-supplied (``extra``) admin is recognized without any file."""
    f = tmp_path / "admins"  # absent
    al = AdminList(f, extra=frozenset({"Alice@Example.com"}))
    assert al.is_admin("alice@example.com")  # config entry, case-insensitive
    assert al.is_admin("ALICE@example.com")
    assert not al.is_admin("bob@example.com")


def test_admin_list_unions_config_and_file(tmp_path: Path) -> None:
    """An identity in EITHER the config set or the file is an admin."""
    f = tmp_path / "admins"
    f.write_text("bob@example.com\n")
    al = AdminList(f, extra=frozenset({"alice@example.com"}))
    assert al.is_admin("alice@example.com")  # from config
    assert al.is_admin("bob@example.com")  # from file


def test_load_admin_list_passes_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_admin_list(extra=…)`` threads the config admins through."""
    monkeypatch.setenv("OMNIGENT_ADMIN_LIST_PATH", "/tmp/omnigent-admins-absent")
    al = load_admin_list(extra=frozenset({"carol@example.com"}))
    assert al.is_admin("carol@example.com")


# ── promote_if_listed against the permission store (OIDC path) ─────


def test_promote_if_listed_permission_store(tmp_path: Path, db_uri: str) -> None:
    """A listed user is promoted to admin in the permission store.

    Mirrors what the OIDC callback does after ``ensure_user``.
    """
    store = SqlAlchemyPermissionStore(db_uri)
    store.ensure_user("alice@example.com")
    assert store.is_admin("alice@example.com") is False

    f = tmp_path / "admins"
    f.write_text("alice@example.com\n")
    admin_list = AdminList(f)

    promoted = promote_if_listed(admin_list, store, "alice@example.com")
    assert promoted is True
    assert store.is_admin("alice@example.com") is True


def test_promote_if_listed_skips_unlisted_user(tmp_path: Path, db_uri: str) -> None:
    """A user absent from the file is left as a non-admin member."""
    store = SqlAlchemyPermissionStore(db_uri)
    store.ensure_user("bob@example.com")

    f = tmp_path / "admins"
    f.write_text("alice@example.com\n")
    admin_list = AdminList(f)

    promoted = promote_if_listed(admin_list, store, "bob@example.com")
    assert promoted is False
    assert store.is_admin("bob@example.com") is False


def test_promote_if_listed_idempotent(tmp_path: Path, db_uri: str) -> None:
    """Re-promoting an already-admin user is a no-op returning False."""
    store = SqlAlchemyPermissionStore(db_uri)
    store.ensure_user("alice@example.com", is_admin=True)

    f = tmp_path / "admins"
    f.write_text("alice@example.com\n")
    admin_list = AdminList(f)

    assert promote_if_listed(admin_list, store, "alice@example.com") is False
    assert store.is_admin("alice@example.com") is True


def test_admin_list_removal_does_not_demote(tmp_path: Path, db_uri: str) -> None:
    """Additive invariant: removing an id from the file never demotes.

    This is the safety property that lets operators edit the file
    freely — you cannot lock the deploy out of its bootstrap admin by
    forgetting to list them.
    """
    store = SqlAlchemyPermissionStore(db_uri)
    store.ensure_user("alice@example.com")

    f = tmp_path / "admins"
    f.write_text("alice@example.com\n")
    admin_list = AdminList(f)
    assert promote_if_listed(admin_list, store, "alice@example.com") is True

    # Operator removes alice from the file.
    f.write_text("")
    stat = f.stat()
    os.utime(f, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    # A subsequent login does not promote (returns False) but the
    # existing admin flag is untouched — no demotion path exists.
    assert promote_if_listed(admin_list, store, "alice@example.com") is False
    assert store.is_admin("alice@example.com") is True


def test_set_admin_promotes_existing_user(db_uri: str) -> None:
    """``PermissionStore.set_admin`` flips an existing row's flag.

    The promotion path needs this because ``ensure_user`` is
    insert-or-nothing and can't change an existing user's flag.
    """
    store = SqlAlchemyPermissionStore(db_uri)
    store.ensure_user("alice@example.com")
    assert store.is_admin("alice@example.com") is False
    store.set_admin("alice@example.com", True)
    assert store.is_admin("alice@example.com") is True


# ── promote_if_listed against the account store (accounts path) ────


def test_promote_if_listed_account_store(tmp_path: Path, db_uri: str) -> None:
    """A listed username is promoted in the account store."""
    store = SqlAlchemyAccountStore(db_uri)
    store.create_user_with_password("carol", hash_password("password123"), is_admin=False)
    assert store.is_admin("carol") is False

    f = tmp_path / "admins"
    f.write_text("carol\n")
    admin_list = AdminList(f)

    assert promote_if_listed(admin_list, store, "carol") is True
    assert store.is_admin("carol") is True


# ── End-to-end: accounts login promotes a listed user ─────────────


@pytest.fixture
def accounts_router_client(
    tmp_path: Path, db_uri: str
) -> Iterator[tuple[TestClient, SqlAlchemyAccountStore, Path]]:
    """A minimal app with only the accounts auth router mounted.

    Exercises the real ``create_accounts_auth_router`` promotion wiring
    without standing up the full ``create_app`` stack. Yields the
    client, the account store (to assert DB state), and the admins-file
    path (to populate before logging in).
    """
    account_store = SqlAlchemyAccountStore(db_uri)
    admins_file = tmp_path / "admins"
    admins_file.write_text("")  # exists, empty by default
    admin_list = AdminList(admins_file)

    config = AccountsConfig(
        cookie_secret=b"\x00" * 32,
        session_ttl_hours=8,
        base_url="http://localhost:8000",
        init_admin_password=None,
        invite_ttl_seconds=72 * 3600,
        magic_ttl_seconds=600,
    )
    provider = UnifiedAuthProvider(source="accounts", accounts_config=config)

    app = FastAPI()
    app.include_router(
        create_accounts_auth_router(provider, account_store, admin_list),
        prefix="/auth",
    )
    with TestClient(app) as client:
        yield client, account_store, admins_file


def test_login_promotes_listed_user_end_to_end(
    accounts_router_client: tuple[TestClient, SqlAlchemyAccountStore, Path],
) -> None:
    """Logging in as a file-listed user promotes them and the response reflects it.

    Proves the wiring in ``create_accounts_auth_router`` calls
    ``promote_if_listed`` on the login path and that the promotion is
    visible both in the DB and in the login response payload.
    """
    client, account_store, admins_file = accounts_router_client
    account_store.create_user_with_password("dave", hash_password("password123"), is_admin=False)
    admins_file.write_text("dave\n")

    resp = client.post("/auth/login", json={"username": "dave", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["user"]["is_admin"] is True
    assert account_store.is_admin("dave") is True


def test_login_unlisted_user_stays_member_end_to_end(
    accounts_router_client: tuple[TestClient, SqlAlchemyAccountStore, Path],
) -> None:
    """A user not in the file logs in as a non-admin member."""
    client, account_store, _admins_file = accounts_router_client
    account_store.create_user_with_password("erin", hash_password("password123"), is_admin=False)

    resp = client.post("/auth/login", json={"username": "erin", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["user"]["is_admin"] is False
    assert account_store.is_admin("erin") is False
