"""Black-box MCP OAuth test against a fake authorization server.

Runs, in-process on loopback, an OAuth authorization server (discovery,
dynamic registration, authorize, token) and an MCP server that only
accepts its bearer tokens. A real :class:`McpServerConnection` signs in,
with the browser step simulated by requesting the authorize URL and
following its redirect to the loopback callback. The fake server behaves
like a strict, compliant one: the token request's ``redirect_uri`` must
equal the authorization request's, PKCE is verified, access tokens expire,
and refresh responses don't rotate the refresh token.

Time is advanced by patching ``time.time`` (client and server share it),
so expiry needs no sleeping.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from omnigent.spec.types import MCPServerConfig
from omnigent.tools import mcp_oauth
from omnigent.tools.mcp import McpServerConnection, clear_discovery_cache
from omnigent.tools.mcp_oauth import McpOAuthError

_ACCESS_TOKEN_LIFETIME_S = 3600


@dataclass
class _FakeAuthServer:
    """State and request log of the fake authorization + MCP server."""

    base: str = ""
    registrations: list[dict[str, Any]] = field(default_factory=list)
    authorize_requests: list[dict[str, str]] = field(default_factory=list)
    token_requests: list[dict[str, str]] = field(default_factory=list)
    clients: dict[str, list[str]] = field(default_factory=dict)
    codes: dict[str, dict[str, str]] = field(default_factory=dict)
    access_tokens: dict[str, float] = field(default_factory=dict)
    refresh_tokens: set[str] = field(default_factory=set)
    mcp_statuses: list[int] = field(default_factory=list)

    def revoke_access_tokens(self) -> None:
        """Invalidate every issued access token (refresh tokens stay valid)."""
        self.access_tokens.clear()

    def grants(self, grant_type: str) -> list[dict[str, str]]:
        return [r for r in self.token_requests if r.get("grant_type") == grant_type]


def _build_app(state: _FakeAuthServer) -> Callable[..., Any]:
    """The ASGI app: OAuth endpoints plus a bearer-protected FastMCP server."""
    mcp_server = FastMCP("fake-oauth-mcp")

    @mcp_server.tool()
    def echo(text: str) -> str:
        return f"echo: {text}"

    mcp_app = mcp_server.streamable_http_app()

    async def protected_resource(_: Request) -> Response:
        return JSONResponse(
            {"resource": f"{state.base}/mcp", "authorization_servers": [f"{state.base}/as"]}
        )

    async def as_metadata(_: Request) -> Response:
        return JSONResponse(
            {
                "issuer": f"{state.base}/as",
                "authorization_endpoint": f"{state.base}/as/authorize",
                "token_endpoint": f"{state.base}/as/token",
                "registration_endpoint": f"{state.base}/as/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            }
        )

    async def register(request: Request) -> Response:
        body = await request.json()
        state.registrations.append(body)
        client_id = f"client-{len(state.registrations)}"
        state.clients[client_id] = list(body["redirect_uris"])
        return JSONResponse({**body, "client_id": client_id}, status_code=201)

    async def authorize(request: Request) -> Response:
        params = dict(request.query_params)
        state.authorize_requests.append(params)
        if params.get("client_id") not in state.clients:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        code = secrets.token_urlsafe(16)
        state.codes[code] = params
        query = urlencode({"code": code, "state": params["state"]})
        return RedirectResponse(f"{params['redirect_uri']}?{query}", status_code=302)

    def issue_tokens(*, include_refresh: bool) -> dict[str, Any]:
        access = secrets.token_urlsafe(16)
        state.access_tokens[access] = time.time() + _ACCESS_TOKEN_LIFETIME_S
        tokens: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": _ACCESS_TOKEN_LIFETIME_S,
        }
        if include_refresh:
            refresh = secrets.token_urlsafe(16)
            state.refresh_tokens.add(refresh)
            tokens["refresh_token"] = refresh
        return tokens

    async def token(request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        state.token_requests.append(form)
        if form.get("grant_type") == "authorization_code":
            grant = state.codes.pop(form.get("code", ""), None)
            if grant is None or form.get("redirect_uri") != grant["redirect_uri"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            if challenge != grant["code_challenge"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(issue_tokens(include_refresh=True))
        if form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") not in state.refresh_tokens:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            # Non-rotating: the client must keep using the same refresh token.
            return JSONResponse(issue_tokens(include_refresh=False))
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    oauth_app = Starlette(
        routes=[
            Route("/.well-known/oauth-protected-resource/mcp", protected_resource),
            Route("/.well-known/oauth-authorization-server/as", as_metadata),
            Route("/as/register", register, methods=["POST"]),
            Route("/as/authorize", authorize),
            Route("/as/token", token, methods=["POST"]),
        ]
    )

    def bearer_is_valid(scope: dict[str, Any]) -> bool:
        headers = dict(scope.get("headers") or [])
        value = headers.get(b"authorization", b"").decode()
        if not value.startswith("Bearer "):
            return False
        expires_at = state.access_tokens.get(value.removeprefix("Bearer "))
        return expires_at is not None and time.time() < expires_at

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await mcp_app(scope, receive, send)
            return
        if scope["path"].startswith("/mcp"):
            if not bearer_is_valid(scope):
                state.mcp_statuses.append(401)
                metadata_url = f"{state.base}/.well-known/oauth-protected-resource/mcp"
                response = Response(
                    status_code=401,
                    headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata_url}"'},
                )
                await response(scope, receive, send)
                return
            state.mcp_statuses.append(200)
            await mcp_app(scope, receive, send)
            return
        await oauth_app(scope, receive, send)

    return app


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_FakeAuthServer]:
    """Serve the fake OAuth + MCP app on a loopback port in a thread."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    clear_discovery_cache()
    state = _FakeAuthServer()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    state.base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(_build_app(state), log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("fake OAuth server did not start")
        time.sleep(0.05)
    try:
        yield state
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        clear_discovery_cache()


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Simulate the user's browser: follow the authorize redirect to the callback."""
    opened: list[str] = []

    def open_browser(url: str) -> bool:
        opened.append(url)
        with httpx.Client(timeout=10) as client:
            redirect = client.get(url)
            assert redirect.status_code == 302, redirect.text
            callback = client.get(redirect.headers["location"])
            assert callback.status_code == 200, callback.text
        return True

    monkeypatch.setattr(mcp_oauth, "_open_browser", open_browser)
    return opened


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Advance wall-clock time (``time.time``) for client and server alike."""
    offset = [0.0]
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + offset[0])

    def advance(seconds: float) -> None:
        offset[0] += seconds

    return advance


def _config(state: _FakeAuthServer) -> MCPServerConfig:
    return MCPServerConfig(
        name="fake", transport="http", url=f"{state.base}/mcp", oauth=True, timeout=20
    )


async def _connect_and_echo(state: _FakeAuthServer, text: str) -> str:
    """One full connection lifetime, as a new process would have it."""
    conn = McpServerConnection(config=_config(state))
    try:
        tools = await asyncio.wait_for(conn.connect(), timeout=30)
        assert [t.name for t in tools] == ["echo"]
        return await asyncio.wait_for(conn.call_tool("echo", {"text": text}), timeout=30)
    finally:
        await conn.close()


async def test_sign_in_reconnect_expiry_and_refresh(
    fake_server: _FakeAuthServer, browser: list[str], clock: Callable[[float], None]
) -> None:
    state = fake_server

    # First connection: discovery, registration, browser sign-in, token exchange.
    assert "echo: one" in await _connect_and_echo(state, "one")
    assert len(browser) == 1
    assert len(state.registrations) == 1
    assert len(state.authorize_requests) == 1
    assert len(state.grants("authorization_code")) == 1
    registered = state.registrations[0]["redirect_uris"]
    authorized = state.authorize_requests[0]["redirect_uri"]
    exchanged = state.grants("authorization_code")[0]["redirect_uri"]
    assert registered == [authorized]
    assert authorized == exchanged
    assert urlparse(authorized).hostname == "127.0.0.1"
    assert urlparse(authorized).port not in (None, 0)
    assert parse_qs(urlparse(browser[0]).query)["code_challenge_method"] == ["S256"]

    # Reconnect (new connection, new provider): stored tokens reused as-is.
    assert "echo: two" in await _connect_and_echo(state, "two")
    conn = McpServerConnection(config=_config(state))
    try:
        await asyncio.wait_for(conn.connect(), timeout=30)
        await asyncio.wait_for(conn._reconnect(), timeout=30)
        assert "echo: three" in await conn.call_tool("echo", {"text": "three"})
    finally:
        await conn.close()
    assert len(browser) == 1
    assert len(state.token_requests) == 1

    # Expiry: the stored token is past its lifetime, so the next connection
    # refreshes it at the discovered token endpoint, without a browser.
    clock(_ACCESS_TOKEN_LIFETIME_S + 60)
    statuses_before = len(state.mcp_statuses)
    assert "echo: four" in await _connect_and_echo(state, "four")
    assert len(state.grants("refresh_token")) == 1
    assert len(browser) == 1
    assert len(state.authorize_requests) == 1
    # Refreshed up front: the expired token was never sent.
    assert 401 not in state.mcp_statuses[statuses_before:]

    # The server rejects a token the client still thinks is valid: a 401
    # triggers a refresh (with the kept, non-rotated refresh token), not a
    # browser sign-in.
    state.revoke_access_tokens()
    assert "echo: five" in await _connect_and_echo(state, "five")
    assert len(state.grants("refresh_token")) == 2
    assert len(browser) == 1
    assert len(state.authorize_requests) == 1
    assert len(state.registrations) == 1


async def test_refresh_token_rejected_falls_back_to_browser_sign_in(
    fake_server: _FakeAuthServer, browser: list[str], clock: Callable[[float], None]
) -> None:
    state = fake_server
    assert "echo: one" in await _connect_and_echo(state, "one")

    state.refresh_tokens.clear()
    clock(_ACCESS_TOKEN_LIFETIME_S + 60)
    assert "echo: two" in await _connect_and_echo(state, "two")

    assert len(browser) == 2
    # The stored client registration is reused, with the same redirect URI.
    assert len(state.registrations) == 1
    second_authorize = state.authorize_requests[1]["redirect_uri"]
    assert second_authorize == state.grants("authorization_code")[1]["redirect_uri"]


async def test_headless_fails_fast_with_a_clear_error(
    fake_server: _FakeAuthServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_oauth, "_browser_available", lambda: False)
    conn = McpServerConnection(config=_config(fake_server))
    started = time.monotonic()
    try:
        with pytest.raises(McpOAuthError, match="can't open a browser"):
            await asyncio.wait_for(conn.connect(), timeout=30)
    finally:
        await conn.close()
    assert time.monotonic() - started < 15
    assert fake_server.authorize_requests == []
    assert fake_server.token_requests == []


def test_fake_server_rejects_a_mismatched_redirect_uri(fake_server: _FakeAuthServer) -> None:
    """Guard the fake: it must enforce redirect_uri equality like a real server."""
    with httpx.Client(timeout=10) as client:
        client_id = client.post(
            f"{fake_server.base}/as/register",
            json={"redirect_uris": ["http://127.0.0.1:0/callback"]},
        ).json()["client_id"]
        redirect = client.get(
            f"{fake_server.base}/as/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:5555/callback",
                "state": "s",
                "code_challenge": "c" * 43,
            },
        )
        code = parse_qs(urlparse(redirect.headers["location"]).query)["code"][0]
        response = client.post(
            f"{fake_server.base}/as/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1:0/callback",
                "code_verifier": "v" * 43,
            },
        )
    assert response.status_code == 400
    assert json.loads(response.text)["error"] == "invalid_grant"
