from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.exceptions import InvalidTag
from omnigent_company_brain.encryption import CredentialCipher
from omnigent_company_brain.oauth import (
    PROVIDERS,
    OAuthStateCodec,
    ResolvedOAuthClient,
    authorize_url,
    exchange_code,
    nonce_digest,
)

_KEY_MATERIAL = "test-key-material-with-more-than-thirty-two-bytes"


def test_credentials_are_bound_to_workspace_and_connection() -> None:
    cipher = CredentialCipher.from_material(_KEY_MATERIAL)
    encrypted = cipher.encrypt_json(
        {"access_token": "access", "refresh_token": "refresh"},
        workspace_id=41,
        connection_id="connection-a",
    )

    assert "access" not in encrypted
    assert cipher.decrypt_json(
        encrypted,
        workspace_id=41,
        connection_id="connection-a",
    ) == {"access_token": "access", "refresh_token": "refresh"}
    with pytest.raises(InvalidTag):
        cipher.decrypt_json(encrypted, workspace_id=42, connection_id="connection-a")
    with pytest.raises(InvalidTag):
        cipher.decrypt_json(encrypted, workspace_id=41, connection_id="connection-b")


def test_oauth_state_rejects_tampering_expiry_and_provider_swap() -> None:
    codec = OAuthStateCodec(_KEY_MATERIAL, max_age_ms=600_000)
    sealed, state = codec.seal(
        provider="google",
        workspace_id=7,
        admin_id="admin@example.com",
        redirect_uri="https://app.example.com/v1/company-brain/oauth/google/callback",
        now_ms=1_000_000,
    )

    assert codec.open(sealed, expected_provider="google", now_ms=1_100_000) == state
    assert len(nonce_digest(state.nonce)) == 64
    with pytest.raises(jwt.InvalidTokenError):
        codec.open(sealed + "tampered", expected_provider="google", now_ms=1_100_000)
    with pytest.raises(ValueError, match="provider mismatch"):
        codec.open(sealed, expected_provider="slack", now_ms=1_100_000)
    with pytest.raises(ValueError, match="expired"):
        codec.open(sealed, expected_provider="google", now_ms=1_700_001)


@pytest.mark.parametrize(
    "return_to",
    ["https://attacker.example/phish", "//attacker.example/phish", "/\\attacker.example"],
)
def test_oauth_state_rejects_cross_origin_return_paths(return_to: str) -> None:
    codec = OAuthStateCodec(_KEY_MATERIAL)

    with pytest.raises(ValueError, match="same-origin"):
        codec.seal(
            provider="google",
            workspace_id=7,
            admin_id="admin@example.com",
            redirect_uri="https://app.example.com/v1/company-brain/oauth/google/callback",
            return_to=return_to,
        )


def test_provider_scopes_exclude_personal_and_write_access() -> None:
    google = set(PROVIDERS["google"].scopes)
    slack = set(PROVIDERS["slack"].scopes)

    assert all("gmail" not in scope for scope in google)
    assert all(not scope.endswith(("/drive", "/calendar")) for scope in google)
    assert not ({"groups:history", "im:history", "mpim:history", "chat:write"} & slack)


def test_google_authorize_url_uses_pkce_and_read_scopes() -> None:
    codec = OAuthStateCodec(_KEY_MATERIAL)
    sealed, state = codec.seal(
        provider="google",
        workspace_id=1,
        admin_id="admin@example.com",
        redirect_uri="https://app.example.com/callback",
    )
    url = authorize_url(
        "google",
        client=ResolvedOAuthClient("client-id", "client-secret"),
        redirect_uri=state.redirect_uri,
        sealed_state=sealed,
        code_verifier=state.code_verifier,
    )
    query = parse_qs(urlparse(url).query)

    assert query["code_challenge_method"] == ["S256"]
    assert "drive.readonly" in query["scope"][0]
    assert "gmail" not in query["scope"][0]


@pytest.mark.asyncio
async def test_provider_specific_token_exchange_shapes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slack.com":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "access_token": "xoxb-token",
                    "scope": "channels:read,channels:history",
                    "team": {"id": "T1", "name": "Example"},
                },
            )
        if request.url.host == "api.notion.com":
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(
                200,
                json={
                    "access_token": "notion-token",
                    "workspace_id": "workspace-1",
                    "workspace_name": "Policies",
                },
            )
        claims = jwt.encode(
            {"hd": "example.com"},
            "not-verified-but-long-enough-for-hs256",
            algorithm="HS256",
        )
        return httpx.Response(
            200,
            json={
                "access_token": "google-token",
                "refresh_token": "google-refresh",
                "expires_in": 3600,
                "scope": "openid email https://www.googleapis.com/auth/drive.readonly",
                "id_token": claims,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        google = await exchange_code(
            "google",
            code="code",
            redirect_uri="https://app.example.com/google",
            code_verifier="v" * 64,
            client_config=ResolvedOAuthClient(
                "google-client", "google-secret", hosted_domain="example.com"
            ),
            http_client=client,
            now_ms=1_000,
        )
        slack = await exchange_code(
            "slack",
            code="code",
            redirect_uri="https://app.example.com/slack",
            code_verifier=None,
            client_config=ResolvedOAuthClient("slack-client", "slack-secret"),
            http_client=client,
            now_ms=1_000,
        )
        notion = await exchange_code(
            "notion",
            code="code",
            redirect_uri="https://app.example.com/notion",
            code_verifier=None,
            client_config=ResolvedOAuthClient("notion-client", "notion-secret"),
            http_client=client,
            now_ms=1_000,
        )

    assert google.refresh_token == "google-refresh"
    assert google.expires_at_ms == 3_601_000
    assert slack.account_label == "Example"
    assert slack.provider_metadata == {"team_id": "T1"}
    assert notion.account_label == "Policies"
    assert json.loads(notion.model_dump_json())["access_token"] == "notion-token"
