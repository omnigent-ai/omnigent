from __future__ import annotations

import httpx
import pytest
import respx
from omnigent_slack.databricks_auth import (
    StateError,
    TokenExchangeError,
    exchange_token,
    sign_state,
    verify_state,
)

_SECRET = "test-state-secret"


def test_sign_verify_roundtrip() -> None:
    state = sign_state("T123", "U456", _SECRET, issued_at=1000)
    result = verify_state(state, _SECRET, now=1000)
    assert result.team_id == "T123"
    assert result.user_id == "U456"
    assert result.issued_at == 1000


def test_verify_rejects_wrong_secret() -> None:
    state = sign_state("T1", "U1", _SECRET, issued_at=1000)
    with pytest.raises(StateError):
        verify_state(state, "different-secret", now=1000)


def test_verify_rejects_tampered_payload() -> None:
    state = sign_state("T1", "U1", _SECRET, issued_at=1000)
    payload_b64, sig = state.split(".", 1)
    # Flip a character in the payload — signature no longer matches.
    tampered = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B") + "." + sig
    with pytest.raises(StateError):
        verify_state(tampered, _SECRET, now=1000)


def test_verify_rejects_expired() -> None:
    state = sign_state("T1", "U1", _SECRET, issued_at=1000)
    with pytest.raises(StateError):
        verify_state(state, _SECRET, ttl_seconds=600, now=2000)


def test_verify_rejects_future_dated() -> None:
    state = sign_state("T1", "U1", _SECRET, issued_at=5000)
    with pytest.raises(StateError):
        verify_state(state, _SECRET, ttl_seconds=600, now=1000)


def test_verify_rejects_malformed() -> None:
    with pytest.raises(StateError):
        verify_state("not-a-valid-token", _SECRET, now=1000)


@pytest.mark.asyncio
@respx.mock
async def test_exchange_token_success() -> None:
    route = respx.post("https://ws.example.com/oidc/v1/token").mock(
        return_value=httpx.Response(200, json={"access_token": "scoped-abc", "expires_in": 3600})
    )
    result = await exchange_token(
        workspace_host="https://ws.example.com",
        subject_token="user-forwarded-token",
        audience="app-client-id",
    )
    assert result.access_token == "scoped-abc"
    assert result.expires_in == 3600
    # Verify the RFC 8693 exchange parameters were sent.
    sent = route.calls.last.request
    body = sent.content.decode()
    assert "grant-type%3Atoken-exchange" in body
    assert "audience=app-client-id" in body
    assert "subject_token=user-forwarded-token" in body


@pytest.mark.asyncio
@respx.mock
async def test_exchange_token_non_200_raises() -> None:
    respx.post("https://ws.example.com/oidc/v1/token").mock(
        return_value=httpx.Response(403, json={"error": "access_denied"})
    )
    with pytest.raises(TokenExchangeError):
        await exchange_token(
            workspace_host="https://ws.example.com",
            subject_token="tok",
            audience="aud",
        )


@pytest.mark.asyncio
@respx.mock
async def test_exchange_token_malformed_body_raises() -> None:
    respx.post("https://ws.example.com/oidc/v1/token").mock(
        return_value=httpx.Response(200, json={"no_token": "here"})
    )
    with pytest.raises(TokenExchangeError):
        await exchange_token(
            workspace_host="https://ws.example.com",
            subject_token="tok",
            audience="aud",
        )
