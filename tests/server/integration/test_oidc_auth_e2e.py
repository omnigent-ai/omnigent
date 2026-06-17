"""Integration tests for OIDC authentication flows.

Exercises the real ``/auth/*`` routes mounted on a FastAPI app with
OIDC auth enabled. The external IdP (token endpoint, userinfo) is
mocked via ``unittest.mock`` so no network calls leave the process.

Covers: login redirect, CLI login/poll, callback token exchange,
logout, and expired-ticket eviction.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.routes.auth import (
    _CLI_TICKET_TTL_SECONDS,
    _CliTicket,
    _evict_expired_tickets,
    create_auth_router,
)

pytestmark = pytest.mark.asyncio

_TEST_SECRET = b"a" * 32
_GITHUB_TOKEN_ENDPOINT = "https://github.com/login/oauth/access_token"


def _make_oidc_config() -> OIDCConfig:
    """Build a minimal GitHub-flavoured OIDCConfig for testing."""
    return OIDCConfig(
        issuer="https://github.com",
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=_TEST_SECRET,
        scopes="read:user user:email",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="github",
        authorization_endpoint="https://github.com/login/oauth/authorize",
        token_endpoint=_GITHUB_TOKEN_ENDPOINT,
        jwks_uri=None,
        userinfo_endpoint="https://api.github.com/user",
        allow_invites=False,
    )


def _build_oidc_app() -> httpx.ASGITransport:
    """Build a minimal FastAPI app with only the OIDC auth router."""
    from fastapi import FastAPI

    config = _make_oidc_config()
    auth_provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
    admin_list = AdminList(Path("/tmp/nonexistent-admin-list.txt"))

    router = create_auth_router(
        auth_provider=auth_provider,
        permission_store=None,
        admin_list=admin_list,
    )

    app = FastAPI()
    app.include_router(router, prefix="/auth")
    return httpx.ASGITransport(app=app)


def _mint_state_cookie(
    state: str,
    code_verifier: str = "test-verifier",
    return_to: str = "/",
    ticket: str | None = None,
) -> str:
    """Mint a signed auth-state cookie matching what /auth/login produces."""
    config = _make_oidc_config()
    payload: dict = {
        "state": state,
        "code_verifier": code_verifier,
        "return_to": return_to,
        "exp": int(time.time()) + 300,
    }
    if ticket:
        payload["ticket"] = ticket
    return jwt.encode(payload, config.cookie_secret, algorithm="HS256")


def _mock_httpx_client_for_github(
    token_status: int = 200,
    token_json: dict | None = None,
    emails_json: list | None = None,
) -> AsyncMock:
    """Build a mock httpx.AsyncClient context manager for GitHub endpoints.

    Returns an AsyncMock suitable for use as an async context manager
    (``async with httpx.AsyncClient() as client``).
    """
    if token_json is None:
        token_json = {"access_token": "gho_test_token", "token_type": "bearer"}
    if emails_json is None:
        emails_json = [{"email": "alice@example.com", "primary": True, "verified": True}]

    token_resp = MagicMock()
    token_resp.status_code = token_status
    token_resp.json.return_value = token_json
    token_resp.text = str(token_json)

    emails_resp = MagicMock()
    emails_resp.status_code = 200
    emails_resp.json.return_value = emails_json

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=token_resp)
    mock_client.get = AsyncMock(return_value=emails_resp)

    # Make it work as an async context manager.
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm


# ── 1. Login redirect ──────────────────────────────────────────────


async def test_login_redirects_to_idp_with_pkce_params() -> None:
    """GET /auth/login returns a 302 to the IdP with PKCE parameters.

    The redirect URL must contain client_id, redirect_uri, state,
    code_challenge, and code_challenge_method=S256.
    """
    transport = _build_oidc_app()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get("/auth/login")

    assert resp.status_code == 302
    location = resp.headers["location"]
    parsed = urlparse(location)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.hostname == "github.com"
    assert parsed.path == "/login/oauth/authorize"
    assert params["client_id"] == ["test-client-id"]
    assert params["redirect_uri"] == ["http://localhost:8000/auth/callback"]
    assert params["code_challenge_method"] == ["S256"]
    assert "state" in params
    assert "code_challenge" in params
    # Auth state cookie must be set.
    assert "ap_auth_state" in resp.cookies


# ── 2. CLI login ───────────────────────────────────────────────────


async def test_cli_login_creates_ticket() -> None:
    """POST /auth/cli-login returns a ticket_id and login URL."""
    transport = _build_oidc_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/auth/cli-login")

    assert resp.status_code == 200
    body = resp.json()
    assert "ticket" in body
    assert "login_url" in body
    assert body["login_url"].startswith("/auth/login?ticket=")


# ── 3. CLI poll (pending) ─────────────────────────────────────────


async def test_cli_poll_returns_pending_before_callback() -> None:
    """GET /auth/cli-poll returns 202 while the ticket is unfulfilled."""
    transport = _build_oidc_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Create a ticket first.
        create_resp = await client.post("/auth/cli-login")
        ticket_id = create_resp.json()["ticket"]

        # Poll -- should be pending.
        poll_resp = await client.get(f"/auth/cli-poll?ticket={ticket_id}")

    assert poll_resp.status_code == 202
    assert poll_resp.json()["status"] == "pending"


async def test_cli_poll_returns_410_for_unknown_ticket() -> None:
    """GET /auth/cli-poll returns 410 for a non-existent ticket."""
    transport = _build_oidc_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/auth/cli-poll?ticket=nonexistent")

    assert resp.status_code == 410


# ── 4. Callback with mocked IdP ───────────────────────────────────


async def test_callback_exchanges_code_and_sets_session_cookie() -> None:
    """GET /auth/callback mocks the IdP token exchange, sets a session cookie.

    Mocks the GitHub token endpoint and emails endpoint. Verifies
    the session cookie is set and the auth state cookie is cleared.
    """
    mock_cm = _mock_httpx_client_for_github()
    transport = _build_oidc_app()
    state = "test-state-value"
    state_cookie = _mint_state_cookie(state)

    # Build the test client BEFORE patching httpx.AsyncClient so the
    # test transport is real; only the route's outbound IdP call is mocked.
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        with patch("omnigent.server.routes.auth.httpx.AsyncClient", return_value=mock_cm):
            resp = await client.get(
                "/auth/callback",
                params={"code": "auth-code-123", "state": state},
                cookies={"ap_auth_state": state_cookie},
            )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
    # Session cookie should be set.
    assert "ap_session" in resp.cookies
    # Validate the session JWT.
    session_jwt = resp.cookies["ap_session"]
    payload = jwt.decode(session_jwt, _TEST_SECRET, algorithms=["HS256"])
    assert payload["sub"] == "alice@example.com"


async def test_callback_returns_400_on_missing_code() -> None:
    """GET /auth/callback without code param returns 400."""
    transport = _build_oidc_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/auth/callback", params={"state": "x"})

    assert resp.status_code == 400
    assert "Missing code or state" in resp.json()["error"]


async def test_callback_returns_400_on_state_mismatch() -> None:
    """GET /auth/callback with mismatched state returns 400."""
    transport = _build_oidc_app()
    state_cookie = _mint_state_cookie("correct-state")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/auth/callback",
            params={"code": "auth-code", "state": "wrong-state"},
            cookies={"ap_auth_state": state_cookie},
        )

    assert resp.status_code == 400
    assert "State mismatch" in resp.json()["error"]


async def test_callback_returns_400_on_token_exchange_failure() -> None:
    """GET /auth/callback returns 400 when the IdP token exchange fails."""
    mock_cm = _mock_httpx_client_for_github(
        token_status=401,
        token_json={"error": "bad_verification_code"},
    )
    transport = _build_oidc_app()
    state = "test-state"
    state_cookie = _mint_state_cookie(state)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("omnigent.server.routes.auth.httpx.AsyncClient", return_value=mock_cm):
            resp = await client.get(
                "/auth/callback",
                params={"code": "bad-code", "state": state},
                cookies={"ap_auth_state": state_cookie},
            )

    assert resp.status_code == 400
    assert "Token exchange failed" in resp.json()["error"]


# ── 5. Logout ──────────────────────────────────────────────────────


async def test_logout_clears_cookie_and_redirects() -> None:
    """GET /auth/logout clears the session cookie and redirects to /."""
    transport = _build_oidc_app()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get("/auth/logout")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
    # The session cookie should be cleared with Max-Age=0 deletion semantics.
    set_cookie_headers = resp.headers.get_list("set-cookie")
    session_cookies = [h for h in set_cookie_headers if "ap_session" in h]
    assert session_cookies, "expected a Set-Cookie header for ap_session"
    assert any("Max-Age=0" in h for h in session_cookies), (
        f"expected Max-Age=0 deletion cookie; got {session_cookies}"
    )


# ── 6. Expired ticket eviction ─────────────────────────────────────


async def test_evict_expired_tickets_removes_old_entries() -> None:
    """_evict_expired_tickets removes tickets older than the TTL."""
    tickets: dict[str, _CliTicket] = {
        "fresh": _CliTicket(created_at=time.time()),
        "expired": _CliTicket(created_at=time.time() - _CLI_TICKET_TTL_SECONDS - 1),
        "also_expired": _CliTicket(created_at=time.time() - _CLI_TICKET_TTL_SECONDS - 100),
    }

    _evict_expired_tickets(tickets)

    assert "fresh" in tickets
    assert "expired" not in tickets
    assert "also_expired" not in tickets


async def test_evict_expired_tickets_noop_when_all_fresh() -> None:
    """_evict_expired_tickets is a no-op when no tickets are expired."""
    tickets: dict[str, _CliTicket] = {
        "a": _CliTicket(created_at=time.time()),
        "b": _CliTicket(created_at=time.time() - 10),
    }

    _evict_expired_tickets(tickets)

    assert len(tickets) == 2
