"""Shared OIDC configuration and route transport; clients stay in the tests."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import jwt
from fastapi import FastAPI

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.oidc import OIDCConfig, derive_code_challenge
from omnigent.server.routes.auth import create_auth_router

# 32-byte HMAC key the tests sign and verify session JWTs with.
TEST_SIGNING_KEY = b"a" * 32


def make_oidc_config() -> OIDCConfig:
    """Build a minimal GitHub-flavoured OIDCConfig for testing."""
    return OIDCConfig(
        issuer="https://github.com",
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=TEST_SIGNING_KEY,
        scopes="read:user user:email",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="github",
        authorization_endpoint="https://github.com/login/oauth/authorize",
        token_endpoint="https://github.com/login/oauth/access_token",
        jwks_uri=None,
        userinfo_endpoint="https://api.github.com/user",
        allow_invites=False,
    )


def build_oidc_transport(
    store: DeviceGrantStore | None = None, *, base_path: str = "", redirect_uri: str | None = None
) -> httpx.ASGITransport:
    config = make_oidc_config()
    if redirect_uri is not None:
        config = replace(config, redirect_uri=redirect_uri)
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
    router = create_auth_router(
        auth_provider=provider,
        permission_store=None,
        admin_list=AdminList(Path("/tmp/nonexistent-admin-list.txt")),
        device_grant_store=store,
    )
    app = FastAPI()
    app.state.base_path = base_path
    app.include_router(router, prefix=f"{base_path}/auth")
    return httpx.ASGITransport(app=app)


def _pkce_pair() -> tuple[str, str]:
    verifier = "a" * 64
    return verifier, derive_code_challenge(verifier)


def _mint_state_cookie(
    state: str,
    code_verifier: str = "test-verifier",
    return_to: str = "/",
    ticket: str | None = None,
) -> str:
    """Mint a signed auth-state cookie matching what /auth/login produces."""
    config = make_oidc_config()
    payload: dict = {
        "state": state,
        "code_verifier": code_verifier,
        "return_to": return_to,
        "exp": int(time.time()) + 300,
    }
    if ticket:
        payload["ticket"] = ticket
    return jwt.encode(payload, config.cookie_secret, algorithm="HS256")


def _mock_httpx_client_for_github(
    token_status: int = 200,
    token_json: object | None = None,
    emails_json: list | None = None,
) -> AsyncMock:
    """Build a mock httpx.AsyncClient context manager for GitHub endpoints.

    Returns an AsyncMock suitable for use as an async context manager
    (``async with httpx.AsyncClient() as client``).
    """
    if token_json is None:
        token_json = {"access_token": "gho_test_token", "token_type": "bearer"}
    if emails_json is None:
        emails_json = [{"email": "alice@example.com", "primary": True, "verified": True}]

    token_resp = MagicMock()
    token_resp.status_code = token_status
    token_resp.json.return_value = token_json
    token_resp.text = str(token_json)

    emails_resp = MagicMock()
    emails_resp.status_code = 200
    emails_resp.json.return_value = emails_json

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=token_resp)
    mock_client.get = AsyncMock(return_value=emails_resp)

    # Make it work as an async context manager.
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm
