"""Tests for :func:`omnigent.server.auth.auth_mode` and the session cookie name.

Both feed the ``auth`` block of ``/.well-known/omnigent.json``, which native
clients read before loading anything to decide how to sign in.
"""

from __future__ import annotations

from starlette.requests import HTTPConnection

from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import AuthProvider, UnifiedAuthProvider, auth_mode
from omnigent.server.oidc import OIDCConfig


def _oidc_config(redirect_uri: str) -> OIDCConfig:
    return OIDCConfig(
        issuer="https://github.com",
        client_id="cid",
        client_secret="secret",
        redirect_uri=redirect_uri,
        cookie_secret=b"s" * 32,
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


class _CustomProvider(AuthProvider):
    def get_user_id(self, request: HTTPConnection) -> str | None:
        return "embedded-user"


def test_auth_mode_names_each_source() -> None:
    oidc = UnifiedAuthProvider(source="oidc", oidc_config=_oidc_config("https://a.test/cb"))
    header = UnifiedAuthProvider(source="header", local_single_user=False)

    assert auth_mode(oidc) == "oidc"
    assert auth_mode(header) == "header"
    assert auth_mode(_CustomProvider()) == "custom"
    assert auth_mode(None) == "none"


def test_auth_mode_names_accounts() -> None:
    config = AccountsConfig(
        cookie_secret=b"s" * 32,
        session_ttl_hours=8,
        base_url="https://a.test",
        init_admin_password=None,
        invite_ttl_seconds=3600,
        magic_ttl_seconds=300,
    )
    provider = UnifiedAuthProvider(source="accounts", accounts_config=config)
    assert auth_mode(provider) == "accounts"
    assert provider.session_cookie_name == "__Host-ap_session"


def test_session_cookie_name_follows_redirect_scheme() -> None:
    https = UnifiedAuthProvider(source="oidc", oidc_config=_oidc_config("https://a.test/cb"))
    http = UnifiedAuthProvider(source="oidc", oidc_config=_oidc_config("http://a.test/cb"))
    header = UnifiedAuthProvider(source="header", local_single_user=False)

    assert https.session_cookie_name == "__Host-ap_session"
    assert http.session_cookie_name == "ap_session"
    assert header.session_cookie_name is None
