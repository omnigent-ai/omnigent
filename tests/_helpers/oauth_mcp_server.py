"""A loopback MCP server behind OAuth, for driving real ``mcp login`` flows hermetically."""

from __future__ import annotations

import html
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import uvicorn
from mcp.server.auth.provider import AccessToken, AuthorizationCode, construct_redirect_uri
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response


@dataclass
class _Provider:
    base: str
    clients: dict[str, Any] = field(default_factory=dict)
    codes: dict[str, AuthorizationCode] = field(default_factory=dict)
    tokens: dict[str, AccessToken] = field(default_factory=dict)
    pending: dict[str, tuple[Any, Any]] = field(default_factory=dict)

    async def get_client(self, client_id: str) -> Any:
        return self.clients.get(client_id)

    async def register_client(self, client_info: Any) -> None:
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client: Any, params: Any) -> str:
        request = secrets.token_hex(8)
        self.pending[request] = (client, params)
        return f"{self.base}/approve?{urlencode({'request': request})}"

    async def load_authorization_code(self, client: Any, code: str) -> AuthorizationCode | None:
        return self.codes.get(code)

    async def exchange_authorization_code(
        self, client: Any, code: AuthorizationCode
    ) -> OAuthToken:
        self.codes.pop(code.code, None)
        token = secrets.token_hex(16)
        self.tokens[token] = AccessToken(
            token=token,
            client_id=client.client_id,
            scopes=code.scopes,
            expires_at=int(time.time()) + 3600,
            resource=code.resource,
        )
        return OAuthToken(access_token=token, token_type="Bearer", expires_in=3600)

    async def load_refresh_token(self, client: Any, refresh_token: str) -> None:
        return None

    async def exchange_refresh_token(self, client: Any, refresh_token: Any, scopes: Any) -> Any:
        raise NotImplementedError

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self.tokens.get(token)

    async def revoke_token(self, token: Any) -> None:
        return None


@dataclass
class OAuthMcpServer:
    """``url`` is the MCP endpoint; ``approve`` plays the user consenting on the page."""

    url: str
    provider: _Provider

    @property
    def tokens_issued(self) -> int:
        return len(self.provider.tokens)

    def approve(self, authorization_url: str) -> str:
        """Follow the consent page as a browser would and return the redirect address."""
        with httpx.Client(follow_redirects=False, timeout=10, trust_env=False) as client:
            consent = client.get(authorization_url).headers["location"]
            return client.post(consent).headers["location"]


@contextmanager
def oauth_mcp_server(title: str = "Demo Issues") -> Iterator[OAuthMcpServer]:
    """Serve an OAuth-protected MCP server (dynamic registration, one tool) on loopback.

    :param title: Name shown on the consent page, e.g. ``"Demo Issues"``.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    provider = _Provider(base)
    app = FastMCP(
        title,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=base,
            resource_server_url=f"{base}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
        ),
    )

    @app.tool()
    def list_issues() -> str:
        """List open issues."""
        return "DEMO-1"

    @app.custom_route("/approve", methods=["GET", "POST"])
    async def approve(request: Request) -> Response:
        key = request.query_params.get("request", "")
        if key not in provider.pending:
            return HTMLResponse("Unknown sign-in request", status_code=400)
        if request.method == "GET":
            client = provider.pending[key][0]
            return HTMLResponse(
                f"<title>Authorize {html.escape(title)}</title>"
                "<body style='font:16px sans-serif;max-width:32rem;margin:4rem auto'>"
                f"<h1>Authorize {html.escape(title)}</h1>"
                f"<p>{html.escape(client.client_name or 'An MCP client')} wants to use your"
                " account. This is a local test OAuth server.</p>"
                f"<form method=post action='/approve?request={html.escape(key)}'>"
                "<button type=submit style='font-size:16px;padding:.5rem 1.5rem'>Approve</button>"
                "</form></body>"
            )
        client, params = provider.pending.pop(key)
        code = secrets.token_hex(16)
        provider.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        target = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
        return RedirectResponse(target, status_code=302)

    server = uvicorn.Server(
        uvicorn.Config(app.streamable_http_app(), log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("OAuth MCP server did not start")
        time.sleep(0.05)
    try:
        yield OAuthMcpServer(f"{base}/mcp", provider)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
