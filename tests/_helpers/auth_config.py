"""
Accounts and OIDC auth configuration for tests: config objects and environment only.

Callers own the signing keys, origins and lifetimes; these builders only place
them in the settings the server reads, so tests that drive HTTP keep their
requests and assertions while the settings stay in one place.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

import pytest

from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.oidc import OIDCConfig

# Fixed, test-only client credential for the stub IdP. The stub never checks it;
# a caller that needs to vary it should add a parameter rather than patch this.
STUB_IDP_CLIENT_SECRET = "client-secret"


def session_auth_config(
    source: str,
    origin: str,
    *,
    signing_key: bytes,
    ttl_hours: int,
    max_hours: int = 720,
    init_admin_password: str | None = None,
) -> AccountsConfig | OIDCConfig:
    """
    Build an accounts or OIDC config that signs session cookies with *signing_key*.

    :param source: ``"accounts"`` or ``"oidc"``.
    :param origin: Public base URL, e.g. ``"https://omnigent.example.com"``.
    :param signing_key: Cookie signing key, at least 32 bytes.
    :param ttl_hours: Idle session lifetime in hours.
    :param max_hours: Absolute session lifetime in hours.
    :param init_admin_password: Accounts-mode seeded admin password, if any.
    :returns: The config for *source*; the OIDC one points at a stub IdP.
    """
    if source == "accounts":
        return AccountsConfig(
            cookie_secret=signing_key,
            session_ttl_hours=ttl_hours,
            base_url=origin,
            init_admin_password=init_admin_password,
            invite_ttl_seconds=3600,
            magic_ttl_seconds=600,
            session_max_lifetime_hours=max_hours,
        )
    return OIDCConfig(
        issuer="https://idp.example.com",
        client_id="cid",
        client_secret=STUB_IDP_CLIENT_SECRET,
        redirect_uri=f"{origin}/auth/callback",
        cookie_secret=signing_key,
        scopes="openid email profile",
        session_ttl_hours=ttl_hours,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="oidc",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token",
        jwks_uri="https://idp.example.com/jwks",
        userinfo_endpoint=None,
        allow_invites=False,
        session_max_lifetime_hours=max_hours,
    )


def accounts_signing_key_from_env() -> bytes:
    """Return the accounts cookie signing key currently set in the environment."""
    return bytes.fromhex(os.environ["OMNIGENT_ACCOUNTS_COOKIE_SECRET"])


def set_accounts_env(monkeypatch: pytest.MonkeyPatch, base_url: str, signing_key_hex: str) -> None:
    """Set the minimal accounts-mode environment, leaving the idle window at its default."""
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", signing_key_hex)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", base_url)
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_SESSION_TTL_HOURS", raising=False)


def set_github_oidc_env(
    monkeypatch: pytest.MonkeyPatch, base_url: str, signing_key_hex: str
) -> None:
    """Set a GitHub OIDC environment; GitHub skips discovery, so ``from_env`` needs no network."""
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://github.com")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_SECRET", STUB_IDP_CLIENT_SECRET)
    monkeypatch.setenv("OMNIGENT_OIDC_REDIRECT_URI", f"{base_url}/auth/callback")
    monkeypatch.setenv("OMNIGENT_OIDC_COOKIE_SECRET", signing_key_hex)
    monkeypatch.delenv("OMNIGENT_OIDC_SESSION_TTL_HOURS", raising=False)


def apply_accounts_server_env(
    env: MutableMapping[str, str],
    *,
    base_url: str,
    signing_key_hex: str,
    admin_username: str,
    admin_password: str,
    ttl_hours: int,
) -> None:
    """
    Configure *env* for an accounts-mode server subprocess with a seeded admin.

    Drops an inherited OIDC issuer and absolute-lifetime override so the
    server uses accounts mode and its default maximum session lifetime.
    """
    env["OMNIGENT_AUTH_PROVIDER"] = "accounts"
    env["OMNIGENT_ACCOUNTS_COOKIE_SECRET"] = signing_key_hex
    env["OMNIGENT_ACCOUNTS_BASE_URL"] = base_url
    env["OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME"] = admin_username
    env["OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD"] = admin_password
    env["OMNIGENT_ACCOUNTS_SESSION_TTL_HOURS"] = str(ttl_hours)
    for name in ("OMNIGENT_OIDC_ISSUER", "OMNIGENT_ACCOUNTS_SESSION_MAX_LIFETIME_HOURS"):
        env.pop(name, None)
