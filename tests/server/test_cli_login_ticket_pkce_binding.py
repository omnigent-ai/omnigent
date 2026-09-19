"""The OIDC CLI login ticket must be bound to the requesting CLI (PKCE).

``omnigent login`` asks the server for a one-time ``ticket``, opens the browser
to the IdP, then polls ``/auth/cli-poll?ticket=T`` for the session token. Nothing
tied the ticket to the CLI that created it and the poll released credentials to
*any* caller that knew the ticket string, so an attacker could mint a ticket,
email ``/auth/login?ticket=T`` to a victim, and — once the victim signed in —
poll the same ticket to steal the victim's session and refresh token.

The fix binds the ticket with PKCE: the CLI sends a code challenge at creation
and must present the matching verifier when polling (a poll with no verifier is
rejected with a 400 "upgrade" error). These pin that binding. On the unpatched
build both fail: ``POST /auth/cli-login`` mints a ticket with no challenge, and
``GET /auth/cli-poll`` without a verifier is accepted (202 pending).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.db.utils import get_or_create_engine
from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.routes.auth import create_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


def _oidc_config() -> OIDCConfig:
    return OIDCConfig(
        issuer="https://sso.example.test",
        client_id="omni-oidc-client",
        client_secret="secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=bytes.fromhex("bb" * 32),
        scopes="openid email profile",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="oidc",
        authorization_endpoint="https://sso.example.test/authorize",
        token_endpoint="https://sso.example.test/token",
        jwks_uri="https://sso.example.test/jwks",
        userinfo_endpoint=None,
        allow_invites=False,
        skip_email_verification=False,
        email_claim="email",
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db_uri = f"sqlite:///{tmp_path}/perms.db"
    get_or_create_engine(db_uri)
    admins = tmp_path / "admins"
    admins.write_text("")
    provider = UnifiedAuthProvider(source="oidc", oidc_config=_oidc_config())
    app = FastAPI()
    app.include_router(
        create_auth_router(provider, SqlAlchemyPermissionStore(db_uri), AdminList(admins)),
        prefix="/auth",
    )
    return TestClient(app, follow_redirects=False)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def test_cli_login_requires_a_pkce_challenge(client: TestClient) -> None:
    """Minting a ticket with no PKCE challenge must be refused."""
    resp = client.post("/auth/cli-login")
    assert resp.status_code >= 400, (
        f"cli-login minted a ticket with no PKCE binding: {resp.status_code} {resp.text}"
    )


def test_cli_poll_requires_the_pkce_verifier(client: TestClient) -> None:
    """A poller that cannot prove it created the ticket gets no answer.

    The ticket is created WITH a challenge (as an upgraded CLI would); polling
    it WITHOUT the matching verifier must be rejected, so an attacker who only
    knows the ticket string cannot retrieve the victim's token.
    """
    _verifier, challenge = _pkce_pair()
    created = client.post(
        "/auth/cli-login",
        json={"code_challenge": challenge, "code_challenge_method": "S256"},
        params={"code_challenge": challenge, "code_challenge_method": "S256"},
    )
    assert created.status_code == 200, created.text
    ticket = created.json()["ticket"]

    polled = client.get("/auth/cli-poll", params={"ticket": ticket})
    assert polled.status_code >= 400, (
        f"cli-poll answered a caller with no PKCE verifier: {polled.status_code} {polled.text}"
    )
