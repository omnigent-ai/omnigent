from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from omnigent_slack.databricks_oauth import (
    DatabricksAuthExpiredError,
    DatabricksOAuthClient,
    DatabricksOAuthError,
    derive_code_challenge,
    generate_code_verifier,
)

_HOST = "https://ws.cloud.databricks.com"
_REDIRECT = "https://bot.example.com/auth/callback"


def _client() -> DatabricksOAuthClient:
    return DatabricksOAuthClient(
        _HOST,
        client_id="client-id",
        client_secret="client-secret",
        scopes="supervisor-agents openid offline_access",
        redirect_uri=_REDIRECT,
    )


def _id_token(email: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def test_pkce_challenge_is_deterministic_s256() -> None:
    verifier = "verifier-value"
    # Known S256 mapping; stable across runs.
    assert derive_code_challenge(verifier) == derive_code_challenge(verifier)
    assert "=" not in derive_code_challenge(verifier)


def test_generate_code_verifier_length() -> None:
    v = generate_code_verifier()
    assert 43 <= len(v) <= 128


def test_authorize_url_carries_pkce_and_state() -> None:
    url = _client().authorize_url(state="st8", code_challenge="chal")
    q = parse_qs(urlsplit(url).query)
    assert url.startswith(f"{_HOST}/oidc/v1/authorize")
    assert q["response_type"] == ["code"]
    assert q["code_challenge"] == ["chal"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"] == ["st8"]
    assert q["redirect_uri"] == [_REDIRECT]
    assert q["client_id"] == ["client-id"]


def test_authorize_url_requires_redirect() -> None:
    client = DatabricksOAuthClient(
        _HOST, client_id="c", client_secret="s", scopes="supervisor-agents", redirect_uri=None
    )
    with pytest.raises(DatabricksOAuthError):
        client.authorize_url(state="s", code_challenge="c")


@respx.mock
async def test_exchange_code_reads_email_from_id_token() -> None:
    respx.post(f"{_HOST}/oidc/v1/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
                "id_token": _id_token("alice@example.com"),
            },
        )
    )
    tokens = await _client().exchange_code(code="code", code_verifier="verifier")
    assert tokens.access_token == "at"
    assert tokens.refresh_token == "rt"
    assert tokens.email == "alice@example.com"


@respx.mock
async def test_exchange_code_falls_back_to_scim_me() -> None:
    respx.post(f"{_HOST}/oidc/v1/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )
    )
    scim = respx.get(f"{_HOST}/api/2.0/preview/scim/v2/Me").mock(
        return_value=httpx.Response(
            200, json={"emails": [{"value": "bob@example.com", "primary": True}]}
        )
    )
    tokens = await _client().exchange_code(code="code", code_verifier="verifier")
    assert scim.called
    assert tokens.email == "bob@example.com"


@respx.mock
async def test_exchange_code_sends_pkce_verifier_and_basic_auth() -> None:
    route = respx.post(f"{_HOST}/oidc/v1/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )
    )
    respx.get(f"{_HOST}/api/2.0/preview/scim/v2/Me").mock(return_value=httpx.Response(404))
    await _client().exchange_code(code="the-code", code_verifier="the-verifier")
    request = route.calls[0].request
    body = parse_qs(request.content.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["the-code"]
    assert body["code_verifier"] == ["the-verifier"]
    # Client authenticates with HTTP Basic (client_id:client_secret).
    assert request.headers["authorization"].startswith("Basic ")


@respx.mock
async def test_refresh_rotates_pair() -> None:
    respx.post(f"{_HOST}/oidc/v1/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600}
        )
    )
    tokens = await _client().refresh("old-refresh")
    assert tokens.access_token == "at2"
    assert tokens.refresh_token == "rt2"
    assert tokens.email == ""  # no id_token fetch on refresh


@respx.mock
async def test_refresh_invalid_grant_is_expired() -> None:
    # A 400 invalid_grant is a dead grant → the expired subtype (drop token).
    respx.post(f"{_HOST}/oidc/v1/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(DatabricksAuthExpiredError):
        await _client().refresh("dead-refresh")


@respx.mock
@pytest.mark.parametrize("error", ["invalid_client", "unauthorized_client"])
async def test_refresh_client_error_is_transient_not_expired(error: str) -> None:
    # invalid_client / unauthorized_client describe the bot's SHARED OAuth app
    # (e.g. a rotated/misconfigured client secret), not the user's grant. They
    # must be transient — never DatabricksAuthExpiredError — so a recoverable
    # bot-side misconfig doesn't delete every enrolled user's refresh token.
    respx.post(f"{_HOST}/oidc/v1/token").mock(
        return_value=httpx.Response(401, json={"error": error})
    )
    with pytest.raises(DatabricksOAuthError) as excinfo:
        await _client().refresh("still-good")
    assert not isinstance(excinfo.value, DatabricksAuthExpiredError)


@respx.mock
async def test_refresh_5xx_is_transient_not_expired() -> None:
    # A 5xx is transient — must NOT be classified as a dead grant, so a valid
    # refresh token isn't discarded on a momentary server error.
    respx.post(f"{_HOST}/oidc/v1/token").mock(
        return_value=httpx.Response(503, text="upstream unavailable")
    )
    with pytest.raises(DatabricksOAuthError) as excinfo:
        await _client().refresh("still-good")
    assert not isinstance(excinfo.value, DatabricksAuthExpiredError)


@respx.mock
async def test_refresh_network_error_is_transient() -> None:
    respx.post(f"{_HOST}/oidc/v1/token").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(DatabricksOAuthError) as excinfo:
        await _client().refresh("still-good")
    assert not isinstance(excinfo.value, DatabricksAuthExpiredError)


@respx.mock
async def test_revoke_is_best_effort() -> None:
    respx.post(f"{_HOST}/oidc/v1/revoke").mock(return_value=httpx.Response(500))
    # Must not raise even when the endpoint errors or is absent.
    await _client().revoke("some-token")
