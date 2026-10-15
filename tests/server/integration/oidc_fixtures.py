"""Shared OIDC configuration and route transport; clients stay in the tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
from fastapi import FastAPI

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.oidc import OIDCConfig
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
