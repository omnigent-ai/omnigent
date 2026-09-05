"""Tests for the Databricks OAuth config / PKCE / payload helpers."""

from __future__ import annotations

import base64
import hashlib

import pytest

from omnigent.server.databricks_app import (
    DatabricksAppError,
    DatabricksConfig,
    authorize_url,
    derive_pkce,
    normalize_workspace_host,
    token_set_from_payload,
)


def _config() -> DatabricksConfig:
    return DatabricksConfig(
        client_id="cid",
        client_secret="sekret",
        redirect_uri="https://omni.example/v1/connections/databricks/callback",
        scopes="all-apis offline_access",
    )


def test_derive_pkce_is_deterministic_and_s256() -> None:
    v1, c1 = derive_pkce("sekret", "nonce-1")
    v2, c2 = derive_pkce("sekret", "nonce-1")
    assert (v1, c1) == (v2, c2)  # deterministic → recomputable at callback
    # Different nonce or secret → different verifier.
    assert derive_pkce("sekret", "nonce-2")[0] != v1
    assert derive_pkce("other", "nonce-1")[0] != v1
    # challenge == base64url(SHA256(verifier)), no padding (RFC 7636 S256).
    expected = base64.urlsafe_b64encode(hashlib.sha256(v1.encode()).digest()).rstrip(b"=").decode()
    assert c1 == expected
    # verifier is in the PKCE charset and length range.
    assert 43 <= len(v1) <= 128 and "=" not in v1


def test_normalize_workspace_host() -> None:
    assert (
        normalize_workspace_host("dbc-abc.cloud.databricks.com")
        == "https://dbc-abc.cloud.databricks.com"
    )
    assert (
        normalize_workspace_host("https://dbc-abc.cloud.databricks.com/")
        == "https://dbc-abc.cloud.databricks.com"
    )
    assert normalize_workspace_host("https://host/some/path") == "https://host"
    assert normalize_workspace_host("http://insecure.example") is None  # non-https rejected
    assert normalize_workspace_host("") is None
    assert normalize_workspace_host("not a url") is None


def test_authorize_url_carries_pkce_and_scopes() -> None:
    url = authorize_url(
        _config(),
        workspace_host="https://dbc-abc.cloud.databricks.com",
        state="STATE",
        code_challenge="CHAL",
    )
    assert url.startswith("https://dbc-abc.cloud.databricks.com/oidc/v1/authorize?")
    for frag in (
        "response_type=code",
        "client_id=cid",
        "code_challenge=CHAL",
        "code_challenge_method=S256",
        "state=STATE",
        "scope=all-apis+offline_access",
    ):
        assert frag in url


def test_exchange_and_refresh_fields() -> None:
    cfg = _config()
    ex = cfg.code_exchange_fields("the-code", code_verifier="ver")
    assert ex["grant_type"] == "authorization_code"
    assert ex["code"] == "the-code" and ex["code_verifier"] == "ver"
    assert ex["client_id"] == "cid" and ex["client_secret"] == "sekret"
    rf = cfg.token_refresh_fields("rt")
    assert rf["grant_type"] == "refresh_token" and rf["refresh_token"] == "rt"


def test_token_set_from_payload() -> None:
    ts = token_set_from_payload(
        {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "all-apis"}
    )
    assert ts.access_token == "at" and ts.refresh_token == "rt"
    assert ts.expires_at is not None and ts.scopes == "all-apis"
    with pytest.raises(DatabricksAppError):
        token_set_from_payload({"error": "invalid_grant", "error_description": "nope"})
    with pytest.raises(DatabricksAppError):
        token_set_from_payload({"token_type": "Bearer"})  # missing access_token


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "OMNIGENT_DATABRICKS_CLIENT_ID",
        "OMNIGENT_DATABRICKS_CLIENT_SECRET",
        "OMNIGENT_DATABRICKS_REDIRECT_URI",
        "OMNIGENT_DOMAIN",
        "OMNIGENT_DATABRICKS_SCOPES",
    ):
        monkeypatch.delenv(k, raising=False)
    assert DatabricksConfig.from_env() is None  # unconfigured → dormant
    monkeypatch.setenv("OMNIGENT_DATABRICKS_CLIENT_ID", "cid")
    monkeypatch.setenv("OMNIGENT_DATABRICKS_CLIENT_SECRET", "sek")
    assert DatabricksConfig.from_env() is None  # no redirect and no domain
    monkeypatch.setenv("OMNIGENT_DOMAIN", "omni.example")
    cfg = DatabricksConfig.from_env()
    assert cfg is not None
    assert cfg.redirect_uri == "https://omni.example/v1/connections/databricks/callback"
    assert cfg.scopes == "all-apis offline_access"
