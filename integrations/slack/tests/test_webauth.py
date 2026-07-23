from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer
from omnigent_slack.config import Settings
from omnigent_slack.databricks_oauth import DatabricksOAuthError, DatabricksTokens
from omnigent_slack.enrollment_state import verify_state
from omnigent_slack.tokens import InMemoryTokenStore
from omnigent_slack.webauth import WebAuthServer

_EMAIL = "user@example.com"
_WORKSPACE = "https://ws.cloud.databricks.com"
# The enrollment state is signed with its own dedicated secret, distinct from
# the OAuth client secret.
_STATE_SECRET = "state-secret-0123456789abcdef0123456789"


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        OMNIGENT_SLACK_BOT_TOKEN="xoxb-x",
        OMNIGENT_SLACK_APP_TOKEN="xapp-x",
        OMNIGENT_SERVER_URL="https://omnigent.example.com",
        OMNIGENT_SLACK_SERVER_AUTH="databricks",
        OMNIGENT_SLACK_WEBAUTH_BASE_URL="https://slackbot.example.com",
        OMNIGENT_SLACK_DATABRICKS_WORKSPACE_HOST=_WORKSPACE,
        OMNIGENT_SLACK_DATABRICKS_CLIENT_ID="client-id",
        OMNIGENT_SLACK_DATABRICKS_CLIENT_SECRET="client-secret",
        OMNIGENT_SLACK_DATABRICKS_STATE_SECRET=_STATE_SECRET,
    )


class FakeOAuthClient:
    """Stands in for DatabricksOAuthClient: records exchange, returns a token.

    ``exchange_result`` is what :meth:`exchange_code` returns (or an exception it
    raises). Records the ``(code, code_verifier)`` it was called with so tests
    can assert the PKCE verifier round-tripped correctly.
    """

    def __init__(self, exchange_result: object) -> None:
        self._result = exchange_result
        self.exchanged: list[tuple[str, str]] = []

    def authorize_url(self, *, state: str, code_challenge: str) -> str:
        return (
            f"{_WORKSPACE}/oidc/v1/authorize?response_type=code&state={state}"
            f"&code_challenge={code_challenge}&code_challenge_method=S256"
        )

    async def exchange_code(self, *, code: str, code_verifier: str) -> DatabricksTokens:
        self.exchanged.append((code, code_verifier))
        if isinstance(self._result, Exception):
            raise self._result
        assert isinstance(self._result, DatabricksTokens)
        return self._result


def _tokens(email: str = _EMAIL) -> DatabricksTokens:
    return DatabricksTokens(
        access_token="access-tok", refresh_token="refresh-tok", expires_in=3600, email=email
    )


def _make_server(exchange_result: object) -> tuple[WebAuthServer, FakeOAuthClient]:
    fake = FakeOAuthClient(exchange_result)
    store = InMemoryTokenStore()
    server = WebAuthServer(_settings(), store, oauth_client=fake)  # type: ignore[arg-type]
    return server, fake


@pytest.fixture
async def harness() -> AsyncIterator[tuple[TestClient, WebAuthServer, InMemoryTokenStore, list]]:
    fake = FakeOAuthClient(_tokens())
    store = InMemoryTokenStore()
    await store.initialize()
    enrolled: list = []

    async def _on_enrolled(team_id: str, user_id: str, server_url: str) -> None:
        enrolled.append((team_id, user_id, server_url))

    server = WebAuthServer(
        _settings(),
        store,
        on_enrolled=_on_enrolled,
        oauth_client=fake,  # type: ignore[arg-type]
    )
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    try:
        yield client, server, store, enrolled
    finally:
        await client.close()


def _issue_link(server: WebAuthServer, *, email: str = _EMAIL) -> str:
    url = server.enrollment_url("T1", "U1", email, "Acme")
    assert url is not None
    return url


def _state_of(link: str) -> str:
    return parse_qs(urlsplit(link).query)["state"][0]


def _confirm_id_of(page_html: str) -> str:
    # The consent page carries the single-use confirm id in a hidden form field.
    match = re.search(r'name="confirm_id" value="([^"]+)"', page_html)
    assert match, "consent page missing confirm_id field"
    return match.group(1)


async def _get_then_confirm(client: TestClient, state: str, code: str = "auth-code"):
    """Run the GET callback (consent) then POST Confirm — the full happy path.

    Returns the POST response. Asserts the GET rendered the consent page (naming
    the identities) and stored nothing yet.
    """
    get_resp = await client.get("/auth/callback", params={"state": state, "code": code})
    assert get_resp.status == 200
    page = await get_resp.text()
    assert "about to connect" in page  # consent, not yet connected
    confirm_id = _confirm_id_of(page)
    return await client.post("/auth/callback", data={"confirm_id": confirm_id})


def test_enrollment_url_points_at_workspace_authorize() -> None:
    server, _ = _make_server(_tokens())
    url = _issue_link(server)
    assert url.startswith(f"{_WORKSPACE}/oidc/v1/authorize")
    # The signed state round-trips the Slack identity + a nonce.
    state = verify_state(_state_of(url), _STATE_SECRET)
    assert state.team_id == "T1" and state.user_id == "U1" and state.email == _EMAIL
    assert state.nonce


def test_enrollment_url_none_without_email() -> None:
    server, _ = _make_server(_tokens())
    assert server.enrollment_url("T1", "U1", "") is None


def test_enrollment_url_none_without_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABRICKS_APP_URL", raising=False)
    settings = _settings().model_copy(update={"databricks_webauth_base_url": None})
    server = WebAuthServer(settings, InMemoryTokenStore(), oauth_client=FakeOAuthClient(_tokens()))  # type: ignore[arg-type]
    assert settings.webauth_base_url is None
    assert server.enrollment_url("T1", "U1", _EMAIL) is None


@pytest.mark.asyncio
async def test_get_shows_consent_and_stores_nothing(harness) -> None:
    # The GET callback exchanges the code but must show a consent page naming the
    # identities and persist NOTHING — storage waits for the confirming POST.
    client, server, store, enrolled = harness
    state = _state_of(_issue_link(server))

    resp = await client.get("/auth/callback", params={"state": state, "code": "auth-code"})
    assert resp.status == 200
    page = await resp.text()
    assert "about to connect" in page
    assert "https://omnigent.example.com" in page  # names the server
    assert _EMAIL in page
    assert '<form method="post"' in page  # a Confirm button that POSTs

    assert await store.get("T1", "U1", "https://omnigent.example.com") is None
    assert enrolled == []


@pytest.mark.asyncio
async def test_confirm_stores_refreshable_token(harness) -> None:
    client, server, store, enrolled = harness
    state = _state_of(_issue_link(server))

    resp = await _get_then_confirm(client, state)
    assert resp.status == 200
    assert "connected" in await resp.text()

    record = await store.get("T1", "U1", "https://omnigent.example.com")
    assert record is not None
    assert record.access_token == "access-tok"
    # The whole point: a refresh token is now stored (no hourly re-enrollment).
    assert record.refresh_token == "refresh-tok"
    assert enrolled == [("T1", "U1", "https://omnigent.example.com")]


@pytest.mark.asyncio
async def test_callback_passes_pkce_verifier_matching_challenge(harness) -> None:
    client, server, _store, _ = harness
    state = _state_of(_issue_link(server))
    await client.get("/auth/callback", params={"state": state, "code": "auth-code"})
    fake = server._oauth  # type: ignore[attr-defined]
    assert fake.exchanged and fake.exchanged[0][0] == "auth-code"
    assert fake.exchanged[0][1]  # a non-empty verifier was supplied


@pytest.mark.asyncio
async def test_confirm_without_prior_get_is_400(harness) -> None:
    # A POST with an unknown/forged confirm id stores nothing.
    client, _server, store, enrolled = harness
    resp = await client.post("/auth/callback", data={"confirm_id": "made-up"})
    assert resp.status == 400
    assert await store.get("T1", "U1", "https://omnigent.example.com") is None
    assert enrolled == []


@pytest.mark.asyncio
async def test_confirm_is_single_use(harness) -> None:
    # Re-submitting the same confirm id (double-click / replay) is refused.
    client, server, _store, _ = harness
    state = _state_of(_issue_link(server))
    get_resp = await client.get("/auth/callback", params={"state": state, "code": "auth-code"})
    confirm_id = _confirm_id_of(await get_resp.text())
    first = await client.post("/auth/callback", data={"confirm_id": confirm_id})
    assert first.status == 200
    second = await client.post("/auth/callback", data={"confirm_id": confirm_id})
    assert second.status == 400


@pytest.mark.asyncio
async def test_callback_email_match_case_insensitive() -> None:
    fake = FakeOAuthClient(_tokens(email="User@Example.com"))
    store = InMemoryTokenStore()
    await store.initialize()
    server = WebAuthServer(_settings(), store, oauth_client=fake)  # type: ignore[arg-type]
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    try:
        state = _state_of(_issue_link(server, email="user@example.com"))
        resp = await _get_then_confirm(client, state, code="c")
        assert resp.status == 200
        assert await store.get("T1", "U1", "https://omnigent.example.com") is not None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_callback_rejects_email_mismatch() -> None:
    # Confused-deputy: the link was issued for the requesting Slack user's email,
    # but the person who actually signed in authenticated as someone else.
    fake = FakeOAuthClient(_tokens(email="victim@example.com"))
    store = InMemoryTokenStore()
    await store.initialize()
    server = WebAuthServer(_settings(), store, oauth_client=fake)  # type: ignore[arg-type]
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    try:
        state = _state_of(_issue_link(server, email="requester@example.com"))
        resp = await client.get("/auth/callback", params={"state": state, "code": "c"})
        assert resp.status == 403
        assert await store.get("T1", "U1", "https://omnigent.example.com") is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_callback_rejects_replayed_link(harness) -> None:
    # The PKCE verifier is single-use: a second callback with the same state
    # finds no verifier and is refused (on top of the code being single-use).
    client, server, _store, _ = harness
    state = _state_of(_issue_link(server))
    first = await client.get("/auth/callback", params={"state": state, "code": "c1"})
    assert first.status == 200
    second = await client.get("/auth/callback", params={"state": state, "code": "c2"})
    assert second.status == 400


@pytest.mark.asyncio
async def test_callback_rejects_bad_state(harness) -> None:
    client, _server, store, _ = harness
    resp = await client.get("/auth/callback", params={"state": "tampered", "code": "c"})
    assert resp.status == 400
    assert await store.get("T1", "U1", "https://omnigent.example.com") is None


@pytest.mark.asyncio
async def test_callback_missing_code_is_400(harness) -> None:
    client, server, _store, _ = harness
    state = _state_of(_issue_link(server))
    resp = await client.get("/auth/callback", params={"state": state})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_callback_oauth_error_param_is_400(harness) -> None:
    client, server, store, _ = harness
    state = _state_of(_issue_link(server))
    resp = await client.get("/auth/callback", params={"state": state, "error": "access_denied"})
    assert resp.status == 400
    assert await store.get("T1", "U1", "https://omnigent.example.com") is None


@pytest.mark.asyncio
async def test_callback_exchange_failure_is_502() -> None:
    server, _fake = _make_server(DatabricksOAuthError("workspace rejected"))
    await server._tokens.initialize()  # type: ignore[attr-defined]
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    try:
        state = _state_of(_issue_link(server))
        resp = await client.get("/auth/callback", params={"state": state, "code": "c"})
        assert resp.status == 502
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_health_ok(harness) -> None:
    client, _server, _store, _ = harness
    resp = await client.get("/health")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_callback_pages_carry_security_headers(harness) -> None:
    # The consent page embeds PII (both emails) + the confirm_id; it must not be
    # cached, framed, or sniffed. Assert on both a 200 (consent) and an error page.
    client, server, _store, _ = harness
    state = _state_of(_issue_link(server))

    consent = await client.get("/auth/callback", params={"state": state, "code": "c"})
    assert consent.headers["Cache-Control"] == "no-store"
    assert consent.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in consent.headers["Content-Security-Policy"]
    assert consent.headers["X-Content-Type-Options"] == "nosniff"

    err = await client.get("/auth/callback", params={"state": "bad", "code": "c"})
    assert err.status == 400
    assert err.headers["Cache-Control"] == "no-store"
