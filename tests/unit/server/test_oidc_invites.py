"""Tests for the opt-in OIDC invite flow.

Covers the pieces that make an individually-invited, off-domain email
admissible:

1. ``OIDCConfig`` parsing of ``OMNIGENT_OIDC_ALLOW_INVITES`` and the
   ``base_url`` derivation used to build invite links.
2. ``SqlAlchemyAccountStore.redeem_oidc_invite`` / ``is_email_invited``
   against a real DB — the OIDC invite reuses the existing
   ``account_tokens`` table (no dedicated table): redemption stamps the
   email into ``user_id`` and that redeemed row is the durable pre-auth.
3. The redeem → admit chain the callback performs (invite token consumed
   once, bound email then admitted past the domain allowlist).
4. The ``POST /auth/invite`` route: admin-gated, mints a usable link,
   and is absent entirely when invites are disabled.

The full browser callback (IdP token exchange) isn't driven here — that
path is covered by the manual IdP verification in the plan; these tests
pin every server-side building block it relies on.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig, mint_session_cookie
from omnigent.server.oidc_access import OidcAdmissionPolicy
from omnigent.server.routes.auth import create_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_TEST_SECRET = bytes.fromhex("aa" * 32)


def _oidc_config(*, allow_invites: bool, allowed_domains: frozenset[str] | None) -> OIDCConfig:
    """Build an OIDCConfig over plain HTTP (so TestClient cookies work)."""
    return OIDCConfig(
        issuer="https://accounts.google.com",
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=_TEST_SECRET,
        scopes="openid email profile",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=allowed_domains,
        provider_type="oidc",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        userinfo_endpoint=None,
        allow_invites=allow_invites,
    )


# ── Config parsing ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    # Follows the project env-var convention (omnigent.server.auth
    # .env_var_is_truthy): 1/true/yes are truthy (case-insensitive);
    # everything else — including "on" — is false.
    [("1", True), ("true", True), ("YES", True), ("on", False), ("0", False), ("", False)],
)
def test_allow_invites_env_parsing(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    """``OMNIGENT_OIDC_ALLOW_INVITES`` parses truthy values; default off.

    Built via the GitHub provider branch so no network discovery runs.
    """
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://github.com")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OMNIGENT_OIDC_REDIRECT_URI", "https://app.example.com/auth/callback")
    monkeypatch.setenv("OMNIGENT_OIDC_COOKIE_SECRET", "aa" * 32)
    monkeypatch.setenv("OMNIGENT_OIDC_ALLOW_INVITES", value)

    config = OIDCConfig.from_env()
    assert config.allow_invites is expected


def test_base_url_derives_from_redirect_uri() -> None:
    """``base_url`` strips the callback path to scheme://host[:port]."""
    config = _oidc_config(allow_invites=True, allowed_domains=None)
    assert config.base_url == "http://localhost:8000"


# ── Store: invite redemption binds email to the account_tokens row ──


def _mint_invite(store: SqlAlchemyAccountStore, *, now: int, ttl: int = 3600) -> str:
    """Mint an invite token (user_id NULL, as the route does) and return its id."""
    token_id = secrets.token_urlsafe(32)
    store.create_token(
        token_id,
        kind="invite",
        user_id=None,
        created_by="admin@example.com",
        created_at=now,
        expires_at=now + ttl,
    )
    return token_id


def test_redeem_oidc_invite_binds_and_is_findable(db_uri: str) -> None:
    """``redeem_oidc_invite`` stamps the email; ``is_email_invited`` finds it.

    Proves the OIDC invite reuses the existing ``account_tokens`` table
    (no dedicated table) — redemption writes the email into ``user_id``
    and that redeemed row is the durable pre-authorization.
    """
    store = SqlAlchemyAccountStore(db_uri)
    now = int(time.time())
    token_id = _mint_invite(store, now=now)

    assert store.is_email_invited("guest@external.com") is False
    assert store.redeem_oidc_invite(token_id, "guest@external.com", now_epoch_seconds=now) is True
    assert store.is_email_invited("guest@external.com") is True


def test_redeem_oidc_invite_is_single_use(db_uri: str) -> None:
    """A token redeems at most once; a second redeem returns False."""
    store = SqlAlchemyAccountStore(db_uri)
    now = int(time.time())
    token_id = _mint_invite(store, now=now)

    assert store.redeem_oidc_invite(token_id, "guest@external.com", now_epoch_seconds=now) is True
    # Second attempt (even by a different email) fails — already redeemed.
    assert store.redeem_oidc_invite(token_id, "other@external.com", now_epoch_seconds=now) is False
    assert store.is_email_invited("other@external.com") is False


def test_redeem_oidc_invite_rejects_expired(db_uri: str) -> None:
    """An expired token can't be redeemed."""
    store = SqlAlchemyAccountStore(db_uri)
    now = int(time.time())
    token_id = _mint_invite(store, now=now, ttl=10)
    assert (
        store.redeem_oidc_invite(token_id, "guest@external.com", now_epoch_seconds=now + 100)
        is False
    )
    assert store.is_email_invited("guest@external.com") is False


def test_accounts_invite_does_not_count_as_oidc_invited(db_uri: str) -> None:
    """An accounts-mode invite (user_id stays NULL) never matches is_email_invited.

    Guards the boundary: only OIDC redemptions stamp ``user_id``, so an
    unredeemed/accounts invite must not admit anyone.
    """
    store = SqlAlchemyAccountStore(db_uri)
    now = int(time.time())
    _mint_invite(store, now=now)  # minted, never redeemed via OIDC
    assert store.is_email_invited("admin@example.com") is False


# ── redeem → admit chain (what the callback does) ──────────────────


def test_redeem_admit_chain(tmp_path: Path, db_uri: str) -> None:
    """A redeemed invite admits the bound email past the domain allowlist.

    Mirrors the callback: atomically redeem+bind the single-use token,
    then confirm the admission policy admits the email despite its
    domain not being allowlisted.
    """
    store = SqlAlchemyAccountStore(db_uri)
    admins = tmp_path / "admins"
    admins.write_text("")
    policy = OidcAdmissionPolicy(
        env_allowed_domains=frozenset({"example.com"}),
        domains_file_path=tmp_path / "allowed_domains",
        admin_list=AdminList(admins),
        invited_lookup=store,
    )

    # Off-domain email is denied before any invite.
    assert policy.is_admitted("guest@external.com") is False

    now = int(time.time())
    token_id = _mint_invite(store, now=now)
    assert store.redeem_oidc_invite(token_id, "guest@external.com", now_epoch_seconds=now) is True

    # Now admitted via the invite bypass, on this and every later login.
    assert policy.is_admitted("guest@external.com") is True


# ── POST /auth/invite route ───────────────────────────────────────


@pytest.fixture
def oidc_invite_client(tmp_path: Path, db_uri: str) -> Iterator[tuple[TestClient, str, AdminList]]:
    """OIDC router with invites enabled, mounted on a TestClient.

    Yields the client, the admin's session JWT (for the Authorization
    header), and the admin list (so tests can list/clear admins).
    """
    perm_store = SqlAlchemyPermissionStore(db_uri)
    account_store = SqlAlchemyAccountStore(db_uri)
    admins = tmp_path / "admins"
    admins.write_text("")
    admin_list = AdminList(admins)

    # An admin principal whose JWT we'll present.
    perm_store.ensure_user("admin@example.com", is_admin=True)

    config = _oidc_config(allow_invites=True, allowed_domains=frozenset({"example.com"}))
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)

    app = FastAPI()
    app.include_router(
        create_auth_router(provider, perm_store, admin_list, account_store),
        prefix="/auth",
    )
    admin_jwt = mint_session_cookie(
        user_id="admin@example.com",
        cookie_secret=config.cookie_secret,
        ttl_hours=8,
        provider="oidc",
    )
    with TestClient(app) as client:
        yield client, admin_jwt, admin_list


def test_invite_route_admin_mints_link(
    oidc_invite_client: tuple[TestClient, str, AdminList],
) -> None:
    """An admin gets a usable invite URL pointing at /auth/login?invite=."""
    client, admin_jwt, _ = oidc_invite_client
    resp = client.post("/auth/invite", headers={"Authorization": f"Bearer {admin_jwt}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["invite_url"].startswith("http://localhost:8000/auth/login?invite=")
    assert body["token"] in body["invite_url"]


def test_invite_route_requires_auth(
    oidc_invite_client: tuple[TestClient, str, AdminList],
) -> None:
    """An unauthenticated caller gets 401."""
    client, _admin_jwt, _ = oidc_invite_client
    resp = client.post("/auth/invite")
    assert resp.status_code == 401


def test_invite_route_rejects_non_admin(
    oidc_invite_client: tuple[TestClient, str, AdminList], db_uri: str
) -> None:
    """A non-admin authenticated user gets 403 (can't mint invites)."""
    client, _admin_jwt, _ = oidc_invite_client
    member_jwt = mint_session_cookie(
        user_id="member@example.com",
        cookie_secret=_TEST_SECRET,
        ttl_hours=8,
        provider="oidc",
    )
    resp = client.post("/auth/invite", headers={"Authorization": f"Bearer {member_jwt}"})
    assert resp.status_code == 403


def test_invite_route_absent_when_disabled(tmp_path: Path, db_uri: str) -> None:
    """With invites disabled, /auth/invite is not mounted (404)."""
    perm_store = SqlAlchemyPermissionStore(db_uri)
    account_store = SqlAlchemyAccountStore(db_uri)
    admins = tmp_path / "admins"
    admins.write_text("")
    config = _oidc_config(allow_invites=False, allowed_domains=None)
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)

    app = FastAPI()
    app.include_router(
        create_auth_router(provider, perm_store, AdminList(admins), account_store),
        prefix="/auth",
    )
    admin_jwt = mint_session_cookie(
        user_id="admin@example.com",
        cookie_secret=config.cookie_secret,
        ttl_hours=8,
        provider="oidc",
    )
    with TestClient(app) as client:
        resp = client.post("/auth/invite", headers={"Authorization": f"Bearer {admin_jwt}"})
    assert resp.status_code == 404
