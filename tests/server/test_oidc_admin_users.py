"""Tests for the read-only OIDC admin user list (``GET /auth/users``).

The OIDC analog of the accounts provider's ``GET /auth/users`` — same
response shape, admin-gated, but read-only (OIDC identities are owned by
the IdP, so there are no password-based management actions). This is the
#1489 fix: under OIDC the SPA otherwise has no admin user surface at all.

Mirrors the harness in ``test_oidc_invites.py`` (real DB + a mounted
OIDC router on a ``TestClient``, presenting the admin's session JWT as a
Bearer token).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig, mint_session_cookie
from omnigent.server.routes.auth import create_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_TEST_SECRET = bytes.fromhex("aa" * 32)


def _oidc_config() -> OIDCConfig:
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
        allowed_domains=None,
        provider_type="oidc",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        userinfo_endpoint=None,
        allow_invites=False,
    )


@pytest.fixture
def oidc_users_client(tmp_path: Path, db_uri: str) -> Iterator[tuple[TestClient, str]]:
    """OIDC router mounted on a TestClient, with two users pre-seeded.

    Yields the client and the admin's session JWT. ``admin@example.com``
    is an admin; ``member@example.com`` is a regular user. The reserved
    ``local`` sentinel is also seeded (must be hidden from the list).
    """
    perm_store = SqlAlchemyPermissionStore(db_uri)
    account_store = SqlAlchemyAccountStore(db_uri)
    admins = tmp_path / "admins"
    admins.write_text("")
    admin_list = AdminList(admins)

    perm_store.ensure_user("admin@example.com", is_admin=True)
    perm_store.ensure_user("member@example.com")
    perm_store.ensure_user("local", is_admin=True)  # reserved sentinel

    config = _oidc_config()
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
        yield client, admin_jwt


def test_users_route_admin_lists_real_users(
    oidc_users_client: tuple[TestClient, str],
) -> None:
    """An admin gets every real user with the admin flag; sentinels hidden."""
    client, admin_jwt = oidc_users_client
    resp = client.get("/auth/users", headers={"Authorization": f"Bearer {admin_jwt}"})
    assert resp.status_code == 200, resp.text
    users = {u["id"]: u for u in resp.json()["users"]}
    # Reserved `local` sentinel is excluded; both real users are present.
    assert set(users) == {"admin@example.com", "member@example.com"}
    assert users["admin@example.com"]["is_admin"] is True
    assert users["member@example.com"]["is_admin"] is False
    # OIDC identities have no password — the shape carries has_password=False.
    assert users["member@example.com"]["has_password"] is False


def test_users_route_requires_auth(
    oidc_users_client: tuple[TestClient, str],
) -> None:
    """An unauthenticated caller gets 401."""
    client, _admin_jwt = oidc_users_client
    resp = client.get("/auth/users")
    assert resp.status_code == 401


def test_users_route_rejects_non_admin(
    oidc_users_client: tuple[TestClient, str],
) -> None:
    """A non-admin authenticated user gets 403 (can't list users)."""
    client, _admin_jwt = oidc_users_client
    member_jwt = mint_session_cookie(
        user_id="member@example.com",
        cookie_secret=_TEST_SECRET,
        ttl_hours=8,
        provider="oidc",
    )
    resp = client.get("/auth/users", headers={"Authorization": f"Bearer {member_jwt}"})
    assert resp.status_code == 403
