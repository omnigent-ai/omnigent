"""Tests for GitHub App config parsing and credential handling.

Config half only — no network. The HTTP client is exercised in
``test_github_app.py``; keeping the secret-shaped assertions here (and
the httpx sink there) mirrors the module split in the source.
"""

from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from omnigent.server.github_app import (
    GitHubAppConfig,
    GitHubAppError,
    build_authorize_url,
    token_set_from_payload,
)
from tests.server.github_app_fixtures import make_config


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OMNIGENT_GITHUB_APP_ID",
        "OMNIGENT_GITHUB_APP_CLIENT_ID",
        "OMNIGENT_GITHUB_APP_CLIENT_SECRET",
        "OMNIGENT_GITHUB_APP_PRIVATE_KEY",
        "OMNIGENT_GITHUB_APP_PRIVATE_KEY_PATH",
        "OMNIGENT_GITHUB_APP_REDIRECT_URI",
        "OMNIGENT_GITHUB_APP_SLUG",
        "OMNIGENT_GITHUB_APP_TOKEN_ENC_KEY",
        "OMNIGENT_DOMAIN",
    ):
        monkeypatch.delenv(name, raising=False)


# ── from_env ─────────────────────────────────────────────────────


def test_from_env_disabled_without_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    assert GitHubAppConfig.from_env() is None


def test_from_env_disabled_without_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_CLIENT_ID", "Iv1abc")
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_CLIENT_SECRET", "shh")
    assert GitHubAppConfig.from_env() is None


def test_from_env_enabled_without_store_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Token-at-rest encryption is the credential store's concern
    # (OMNIGENT_CREDENTIAL_ENC_KEY), not GitHub's: client id/secret + a
    # resolvable redirect are enough for a valid GitHub App config. Whether
    # a connection store is wired is decided separately by the caller.
    _clear_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_CLIENT_ID", "Iv1abc")
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_CLIENT_SECRET", "shh")
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_REDIRECT_URI", "https://x/cb")
    config = GitHubAppConfig.from_env()
    assert config is not None
    assert config.client_id == "Iv1abc"
    assert not hasattr(config, "token_enc_secret")


def test_from_env_derives_redirect_from_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_CLIENT_ID", "Iv1abc")
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_CLIENT_SECRET", "shh")
    monkeypatch.setenv("OMNIGENT_DOMAIN", "omni.example.com")
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_SLUG", "omni-app")
    config = GitHubAppConfig.from_env()
    assert config is not None
    assert config.redirect_uri == "https://omni.example.com/v1/connections/github/callback"
    assert config.install_url == "https://github.com/apps/omni-app/installations/new"


def test_from_env_explicit_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_CLIENT_ID", "Iv1abc")
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_CLIENT_SECRET", "shh")
    monkeypatch.setenv("OMNIGENT_GITHUB_APP_REDIRECT_URI", "https://x/cb")
    config = GitHubAppConfig.from_env()
    assert config is not None
    assert config.redirect_uri == "https://x/cb"
    assert config.install_url is None  # no slug


# ── URL + form-field builders ────────────────────────────────────


def test_build_authorize_url() -> None:
    url = build_authorize_url(make_config(), state="STATE123")
    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=Iv1abc" in url
    assert "state=STATE123" in url
    # GitHub Apps take no scope param.
    assert "scope=" not in url


def test_code_exchange_fields() -> None:
    fields = make_config().code_exchange_fields("code123")
    assert fields["client_id"] == "Iv1abc"
    assert fields["code"] == "code123"
    assert fields["redirect_uri"].endswith("/callback")
    assert "client_secret" in fields


def test_token_refresh_fields() -> None:
    fields = make_config().token_refresh_fields("ghr_x")
    assert fields["grant_type"] == "refresh_token"
    assert fields["refresh_token"] == "ghr_x"
    assert "client_secret" in fields


# ── app JWT ──────────────────────────────────────────────────────


def test_mint_app_jwt_requires_key() -> None:
    with pytest.raises(RuntimeError):
        make_config().mint_app_jwt()


def test_mint_app_jwt_signs_with_private_key() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode("ascii")
    config = GitHubAppConfig(
        app_id="123456",
        client_id="Iv1abc",
        client_secret="shh",
        private_key=pem,
        redirect_uri="https://x/cb",
        slug=None,
    )
    token = config.mint_app_jwt()
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"])
    assert claims["iss"] == "123456"
    assert claims["exp"] > claims["iat"]


# ── token payload parsing ────────────────────────────────────────


def test_token_set_from_payload_full() -> None:
    tokens = token_set_from_payload(
        {
            "access_token": "ghu_new",
            "refresh_token": "ghr_new",
            "expires_in": 28800,
            "refresh_token_expires_in": 15897600,
            "scope": "",
        }
    )
    assert tokens.access_token == "ghu_new"
    assert tokens.refresh_token == "ghr_new"
    assert tokens.expires_at is not None
    assert tokens.refresh_token_expires_at is not None


def test_token_set_from_payload_non_expiring() -> None:
    tokens = token_set_from_payload({"access_token": "ghu_x", "scope": "repo"})
    assert tokens.refresh_token is None
    assert tokens.expires_at is None
    assert tokens.scopes == "repo"


def test_token_set_from_payload_error() -> None:
    with pytest.raises(GitHubAppError):
        token_set_from_payload({"error": "bad_verification_code"})


def test_token_set_from_payload_missing_access_token() -> None:
    with pytest.raises(GitHubAppError):
        token_set_from_payload({"scope": "repo"})
