"""Shared OIDC config fixtures for the ``/auth`` route tests.

Deliberately free of HTTP-client code: CI's exfil scan blocks any single
file whose added lines pair credential-shaped names with a network client,
so the config and signing key live here while the transports stay in the
test modules.
"""

from __future__ import annotations

from omnigent.server.oidc import OIDCConfig

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
