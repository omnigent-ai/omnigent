from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from aiohttp.test_utils import TestClient, TestServer
from omnigent_slack.config import Settings
from omnigent_slack.enrollment_state import (
    FORWARDED_ACCESS_TOKEN_HEADER,
    FORWARDED_EMAIL_HEADER,
    sign_state,
)
from omnigent_slack.tokens import InMemoryTokenStore
from omnigent_slack.webauth import WebAuthServer

_STATE_SECRET = "state-secret"


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        OMNIGENT_SLACK_BOT_TOKEN="xoxb-x",
        OMNIGENT_SLACK_APP_TOKEN="xapp-x",
        OMNIGENT_SERVER_URL="https://omnigent.example.com",
        OMNIGENT_SLACK_SERVER_AUTH="databricks",
        OMNIGENT_SLACK_DATABRICKS_STATE_SECRET=_STATE_SECRET,
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


_EMAIL = "user@example.com"


def _headers(token: str = "forwarded-user-token", email: str = _EMAIL) -> dict[str, str]:
    return {FORWARDED_ACCESS_TOKEN_HEADER: token, FORWARDED_EMAIL_HEADER: email}


def test_enrollment_url_signed_and_pointed_at_base() -> None:
    server = WebAuthServer(_settings(), InMemoryTokenStore())
    url = server.enrollment_url("T1", "U1", _EMAIL)
    assert url is not None
    assert url.startswith("https://slackbot.example.com/auth/callback?state=")


def test_enrollment_url_none_without_email() -> None:
    # No email → no verifiable link (fail closed), even with base + secret set.
    server = WebAuthServer(_settings(), InMemoryTokenStore())
    assert server.enrollment_url("T1", "U1", "") is None


def test_enrollment_url_none_without_base(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail closed when no public base URL is configured. webauth_base_url falls
    # back to the DATABRICKS_APP_URL env var, so clear it — otherwise a value in
    # the runner's environment would make this assert nothing and silently pass.
    monkeypatch.delenv("DATABRICKS_APP_URL", raising=False)
    settings = _settings().model_copy(update={"databricks_webauth_base_url": None})
    server = WebAuthServer(settings, InMemoryTokenStore())
    assert settings.webauth_base_url is None
    assert server.enrollment_url("T1", "U1", _EMAIL) is None


@pytest.mark.asyncio
async def test_get_shows_consent_and_stores_nothing(harness) -> None:
    # The GET landing shows a consent page naming the identities but must NOT
    # persist a token — storage only happens on the confirming POST.
    client, store, enrolled = harness
    state = sign_state("T1", "U1", _EMAIL, _STATE_SECRET, team_name="Acme")

    resp = await client.get("/auth/callback", params={"state": state}, headers=_headers())
    assert resp.status == 200
    body = await resp.text()
    assert "about to connect" in body
    assert "https://omnigent.example.com" in body  # names the server
    assert _EMAIL in body
    assert '<form method="post"' in body  # a Confirm button that POSTs

    assert await store.get("T1", "U1", "https://omnigent.example.com") is None
    assert enrolled == []


@pytest.mark.asyncio
async def test_post_confirm_stores_forwarded_token(harness) -> None:
    client, store, enrolled = harness
    state = sign_state("T1", "U1", _EMAIL, _STATE_SECRET)

    resp = await client.post("/auth/callback", params={"state": state}, headers=_headers())
    assert resp.status == 200
    body = await resp.text()
    assert "connected" in body

    record = await store.get("T1", "U1", "https://omnigent.example.com")
    assert record is not None
    # The forwarded user token is stored directly (Databricks OBO); no exchange.
    assert record.access_token == "forwarded-user-token"
    # Empty refresh: re-enroll on expiry (no refresh token stored).
    assert record.refresh_token == ""
    assert enrolled == [("T1", "U1", "https://omnigent.example.com")]


@pytest.mark.asyncio
async def test_post_confirm_email_match_is_case_insensitive(harness) -> None:
    client, store, _ = harness
    state = sign_state("T1", "U1", "User@Example.com", _STATE_SECRET)
    resp = await client.post(
        "/auth/callback", params={"state": state}, headers=_headers(email="user@example.com")
    )
    assert resp.status == 200
    assert await store.get("T1", "U1", "https://omnigent.example.com") is not None


@pytest.mark.asyncio
async def test_post_confirm_rejects_email_mismatch(harness) -> None:
    # Confused-deputy guard: the browser (victim) email differs from the email
    # the link was issued for (attacker). Must refuse and store nothing — even
    # on the POST (validation is re-run, never trusting that a GET happened).
    client, store, enrolled = harness
    state = sign_state("T1", "ATTACKER", "attacker@example.com", _STATE_SECRET)
    resp = await client.post(
        "/auth/callback",
        params={"state": state},
        headers=_headers(token="victim-token", email="victim@example.com"),
    )
    assert resp.status == 403
    assert await store.get("T1", "ATTACKER", "https://omnigent.example.com") is None
    assert enrolled == []


@pytest.mark.asyncio
async def test_get_consent_rejects_email_mismatch(harness) -> None:
    # The mismatch is caught at the consent step too, so a victim never even
    # sees a Confirm button for someone else's link.
    client, _, _ = harness
    state = sign_state("T1", "ATTACKER", "attacker@example.com", _STATE_SECRET)
    resp = await client.get(
        "/auth/callback",
        params={"state": state},
        headers=_headers(email="victim@example.com"),
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_callback_rejects_bad_state(harness) -> None:
    client, store, _ = harness
    resp = await client.post("/auth/callback", params={"state": "tampered"}, headers=_headers())
    assert resp.status == 400
    assert await store.get("T1", "U1", "https://omnigent.example.com") is None


@pytest.mark.asyncio
async def test_callback_missing_forwarded_token_is_401(harness) -> None:
    client, store, _ = harness
    state = sign_state("T1", "U1", _EMAIL, _STATE_SECRET)
    # No x-forwarded-* headers — proxy/user-auth misconfiguration.
    resp = await client.post("/auth/callback", params={"state": state})
    assert resp.status == 401
    assert await store.get("T1", "U1", "https://omnigent.example.com") is None


@pytest.mark.asyncio
async def test_callback_missing_forwarded_email_is_401(harness) -> None:
    client, store, _ = harness
    state = sign_state("T1", "U1", _EMAIL, _STATE_SECRET)
    # Token present but no email header — can't verify identity, fail closed.
    resp = await client.post(
        "/auth/callback",
        params={"state": state},
        headers={FORWARDED_ACCESS_TOKEN_HEADER: "tok"},
    )
    assert resp.status == 401
    assert await store.get("T1", "U1", "https://omnigent.example.com") is None


@pytest.mark.asyncio
async def test_health_ok(harness) -> None:
    client, _, _ = harness
    resp = await client.get("/health")
    assert resp.status == 200
