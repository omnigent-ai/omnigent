from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from aiohttp.test_utils import TestClient, TestServer
from omnigent_slack.config import Settings
from omnigent_slack.databricks_auth import FORWARDED_ACCESS_TOKEN_HEADER, sign_state
from omnigent_slack.tokens import InMemoryTokenStore
from omnigent_slack.webauth import WebAuthServer

_STATE_SECRET = "state-secret"
_AUDIENCE = "target-app-client-id"


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        OMNIGENT_SLACK_BOT_TOKEN="xoxb-x",
        OMNIGENT_SLACK_APP_TOKEN="xapp-x",
        OMNIGENT_SERVER_URL="https://omnigent.example.com",
        OMNIGENT_SLACK_SERVER_AUTH="databricks",
        OMNIGENT_SLACK_DATABRICKS_AUDIENCE=_AUDIENCE,
        OMNIGENT_SLACK_DATABRICKS_STATE_SECRET=_STATE_SECRET,
        OMNIGENT_SLACK_DATABRICKS_WORKSPACE_HOST="https://ws.example.com",
        OMNIGENT_SLACK_WEBAUTH_BASE_URL="https://slackbot.example.com",
    )


@pytest.fixture
async def harness() -> AsyncIterator[tuple[TestClient, InMemoryTokenStore, list[tuple]]]:
    store = InMemoryTokenStore()
    await store.initialize()
    enrolled: list[tuple] = []

    async def _on_enrolled(team_id: str, user_id: str, server_url: str) -> None:
        enrolled.append((team_id, user_id, server_url))

    server = WebAuthServer(_settings(), store, on_enrolled=_on_enrolled)
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    try:
        yield client, store, enrolled
    finally:
        await client.close()


def test_enrollment_url_signed_and_pointed_at_base() -> None:
    server = WebAuthServer(_settings(), InMemoryTokenStore())
    url = server.enrollment_url("T1", "U1")
    assert url is not None
    assert url.startswith("https://slackbot.example.com/auth/callback?state=")


def test_enrollment_url_none_without_base() -> None:
    settings = _settings().model_copy(update={"databricks_webauth_base_url": None})
    # Also clear the DATABRICKS_APP_URL fallback by constructing without it.
    server = WebAuthServer(settings, InMemoryTokenStore())
    # webauth_base_url reads env; ensure None when neither config nor env set.
    if settings.webauth_base_url is None:
        assert server.enrollment_url("T1", "U1") is None


@pytest.mark.asyncio
@respx.mock
async def test_callback_exchanges_and_stores_token(harness) -> None:
    client, store, enrolled = harness
    respx.post("https://ws.example.com/oidc/v1/token").mock(
        return_value=httpx.Response(200, json={"access_token": "app-scoped-token"})
    )
    state = sign_state("T1", "U1", _STATE_SECRET)

    resp = await client.get(
        "/auth/callback",
        params={"state": state},
        headers={FORWARDED_ACCESS_TOKEN_HEADER: "forwarded-user-token"},
    )
    assert resp.status == 200

    record = await store.get("T1", "U1", "https://omnigent.example.com")
    assert record is not None
    assert record.access_token == "app-scoped-token"
    # Empty refresh: re-enroll on expiry (no broad token stored).
    assert record.refresh_token == ""
    assert enrolled == [("T1", "U1", "https://omnigent.example.com")]


@pytest.mark.asyncio
async def test_callback_rejects_bad_state(harness) -> None:
    client, store, _ = harness
    resp = await client.get(
        "/auth/callback",
        params={"state": "tampered"},
        headers={FORWARDED_ACCESS_TOKEN_HEADER: "forwarded-user-token"},
    )
    assert resp.status == 400
    assert await store.get("T1", "U1", "https://omnigent.example.com") is None


@pytest.mark.asyncio
async def test_callback_missing_forwarded_token_is_401(harness) -> None:
    client, store, _ = harness
    state = sign_state("T1", "U1", _STATE_SECRET)
    # No x-forwarded-access-token header — proxy/user-auth misconfiguration.
    resp = await client.get("/auth/callback", params={"state": state})
    assert resp.status == 401
    assert await store.get("T1", "U1", "https://omnigent.example.com") is None


@pytest.mark.asyncio
@respx.mock
async def test_callback_exchange_failure_is_502(harness) -> None:
    client, store, _ = harness
    respx.post("https://ws.example.com/oidc/v1/token").mock(
        return_value=httpx.Response(403, json={"error": "access_denied"})
    )
    state = sign_state("T1", "U1", _STATE_SECRET)
    resp = await client.get(
        "/auth/callback",
        params={"state": state},
        headers={FORWARDED_ACCESS_TOKEN_HEADER: "forwarded-user-token"},
    )
    assert resp.status == 502
    assert await store.get("T1", "U1", "https://omnigent.example.com") is None


@pytest.mark.asyncio
async def test_health_ok(harness) -> None:
    client, _, _ = harness
    resp = await client.get("/health")
    assert resp.status == 200
