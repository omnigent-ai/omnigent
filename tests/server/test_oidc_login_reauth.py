"""OIDC /auth/login must forward prompt=login on reauth=1 — anti-phishing gate guard.

The device-grant consent page's anti-phishing gate rejects a session whose
``iat`` predates the grant's ``created_at``.  When a stale session is detected
the gate bounces the browser to ``/auth/login?reauth=1``, expecting the login
page to force re-authentication.

In accounts mode ``?reauth=1`` is honoured by the SPA login form (it shows the
password field even when the user is already signed in).  In OIDC mode
``/auth/login`` must forward a forced-re-authentication demand to the IdP —
``prompt=login`` plus ``max_age=0`` — so the IdP cannot silently reuse an
existing IdP session.

``/auth/login`` must both (a) forward ``prompt=login`` + ``max_age=0`` to the
IdP and (b) stamp ``reauth_at`` into the signed state cookie so the callback can
verify the id_token's ``auth_time`` proves a real re-authentication.  Without
(a) the bypass was:

    stale session → bounce /auth/login?reauth=1
      → IdP recognises its own live session
      → silent redirect back with a fresh code
      → callback mints a new session (fresh iat)
      → consent page's iat ≥ created_at check passes
      → user approves without actually re-authenticating

These guard (a) and (b); the callback-side ``auth_time`` enforcement (which
closes the residual "IdP ignored prompt=login" hole) is covered in
``test_oidc_callback.py``.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.routes.auth import create_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_ISSUER = "https://sso.example.test"
_CLIENT_ID = "omni-oidc-client"
_AUTHORIZATION_ENDPOINT = f"{_ISSUER}/authorize"
_TEST_SECRET = bytes.fromhex("bb" * 32)


def _oidc_config() -> OIDCConfig:
    """Minimal OIDC config for the login-redirect tests."""
    return OIDCConfig(
        issuer=_ISSUER,
        client_id=_CLIENT_ID,
        client_secret="secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=_TEST_SECRET,
        scopes="openid email profile",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="oidc",
        authorization_endpoint=_AUTHORIZATION_ENDPOINT,
        token_endpoint=f"{_ISSUER}/token",
        jwks_uri=f"{_ISSUER}/jwks",
        userinfo_endpoint=None,
        allow_invites=False,
        skip_email_verification=False,
        email_claim="email",
    )


@pytest.fixture
def login_client(
    tmp_path,
    db_uri: str,
) -> TestClient:
    """Mount the OIDC auth router; return a TestClient for /auth/login probes."""
    from omnigent.server.admin_list import AdminList

    admins = tmp_path / "admins"
    admins.write_text("")
    perm_store = SqlAlchemyPermissionStore(db_uri)
    provider = UnifiedAuthProvider(source="oidc", oidc_config=_oidc_config())

    app = FastAPI()
    app.include_router(
        create_auth_router(provider, perm_store, AdminList(admins)),
        prefix="/auth",
    )
    with TestClient(app, follow_redirects=False) as client:
        return client


def _parse_auth_url(location: str) -> dict[str, list[str]]:
    """Return the query parameters of the authorization-endpoint redirect."""
    parsed = urlsplit(location)
    assert parsed.netloc in location, f"unexpected redirect: {location!r}"
    return parse_qs(parsed.query)


def test_login_without_reauth_sends_no_prompt(login_client: TestClient) -> None:
    """A normal /auth/login (no reauth) sends no prompt param to the IdP.

    Baseline: the fix must not add prompt=login to plain logins.
    """
    r = login_client.get("/auth/login")
    assert r.status_code == 302, f"expected 302, got {r.status_code}"
    location = r.headers["location"]
    params = _parse_auth_url(location)
    assert "prompt" not in params, (
        "plain /auth/login must NOT include prompt in the authorization URL"
    )
    assert "max_age" not in params, (
        "plain /auth/login must NOT include max_age in the authorization URL"
    )


def test_login_with_reauth_forces_prompt_login_at_idp(login_client: TestClient) -> None:
    """OIDC /auth/login?reauth=1 must forward prompt=login + max_age=0 to the IdP.

    This is the anti-phishing gate: when the device-grant consent page detects a
    stale session it bounces here with reauth=1.  Without prompt=login the IdP
    silently reuses its own session, mints a new callback code, and the consent
    page's freshness check passes — the user approves without re-authenticating.

    Fail→pass guard.  Without the fix, /auth/login never reads reauth=1 and emits no prompt param.
    """
    r = login_client.get("/auth/login", params={"reauth": "1"})
    assert r.status_code == 302, f"expected 302, got {r.status_code}"
    location = r.headers["location"]
    params = _parse_auth_url(location)

    assert params.get("prompt") == ["login"], (
        "/auth/login?reauth=1 must add prompt=login to the IdP authorization URL "
        "so the IdP cannot silently reuse an existing session; "
        f"got authorization URL: {location!r}"
    )
    assert params.get("max_age") == ["0"], (
        "/auth/login?reauth=1 must add max_age=0 to the IdP authorization URL "
        f"got authorization URL: {location!r}"
    )


@pytest.fixture
def github_client(tmp_path, db_uri: str) -> TestClient:
    """Mount the auth router for a GitHub OAuth provider."""
    from omnigent.server.admin_list import AdminList

    admins = tmp_path / "admins"
    admins.write_text("")
    perm_store = SqlAlchemyPermissionStore(db_uri)
    # GitHub OAuth uses _source="oidc" but provider_type="github".
    github_cfg = _oidc_config()
    # Patch provider_type to github for this fixture.
    import dataclasses

    github_cfg = dataclasses.replace(github_cfg, provider_type="github")
    provider = UnifiedAuthProvider(source="oidc", oidc_config=github_cfg)
    app = FastAPI()
    app.include_router(
        create_auth_router(provider, perm_store, AdminList(admins)),
        prefix="/auth",
    )
    with TestClient(app, follow_redirects=False) as client:
        return client


def test_login_with_reauth_stamps_reauth_at_in_state(login_client: TestClient) -> None:
    """OIDC /auth/login?reauth=1 records reauth_at in the signed state cookie.

    The callback needs a timestamp to compare the id_token's auth_time
    against, so it can tell a genuine re-authentication from an IdP that
    silently reused its session. This proves the marker is written (and is
    absent on a plain login).
    """
    import jwt

    r = login_client.get("/auth/login", params={"reauth": "1"})
    assert r.status_code == 302
    state_cookie = r.cookies.get("ap_auth_state") or r.cookies.get("__Host-ap_auth_state")
    assert state_cookie is not None, "login must set the auth-state cookie"
    payload = jwt.decode(state_cookie, _TEST_SECRET, algorithms=["HS256"])
    assert isinstance(payload.get("reauth_at"), int), (
        "reauth login must stamp an integer reauth_at into the state cookie"
    )

    # A plain login must NOT stamp it.
    r2 = login_client.get("/auth/login")
    plain_cookie = r2.cookies.get("ap_auth_state") or r2.cookies.get("__Host-ap_auth_state")
    assert plain_cookie is not None
    plain_payload = jwt.decode(plain_cookie, _TEST_SECRET, algorithms=["HS256"])
    assert "reauth_at" not in plain_payload


def test_github_provider_reauth_sends_no_prompt(github_client: TestClient) -> None:
    """GitHub OAuth does not support prompt=login; reauth=1 must be silently ignored.

    The device-grant router refuses to mount for GitHub (enforced in
    create_device_auth_router), so /auth/login?reauth=1 should not reach a GitHub
    deployment from the device-grant consent path. But if it does, the handler
    must not add prompt=login (GitHub ignores it; some implementations error).
    """
    r = github_client.get("/auth/login", params={"reauth": "1"})
    assert r.status_code == 302, f"expected 302, got {r.status_code}"
    location = r.headers["location"]
    params = _parse_auth_url(location)
    assert "prompt" not in params, (
        "GitHub OAuth /auth/login must NOT include prompt (GitHub ignores it); "
        f"got authorization URL: {location!r}"
    )
    assert "max_age" not in params, (
        f"GitHub OAuth /auth/login must NOT include max_age; got authorization URL: {location!r}"
    )
