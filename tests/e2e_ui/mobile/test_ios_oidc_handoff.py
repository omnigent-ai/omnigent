"""E2E coverage for the iOS system-browser OIDC session handoff.

The iOS shell and Safari have isolated cookie stores. The native shell starts
the production browser-ticket flow, Safari completes OIDC, and the shell polls
the ticket before installing the returned session JWT in its WebView.

Playwright cannot execute UIKit or ``WKNavigationDelegate`` on Linux CI, so this
test covers the shared production contract at the browser boundary: real auth
routes, a browser-driven IdP redirect, a separately polled ticket, and an
isolated WebView-like context that becomes authenticated only after receiving
the polled session.
"""

from __future__ import annotations

import html
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest
import uvicorn
from fastapi import FastAPI, Request
from playwright.sync_api import Browser, Playwright, expect
from starlette.responses import HTMLResponse

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.routes import auth as auth_routes
from omnigent.server.routes.auth import create_auth_router
from tests.e2e_ui.conftest import _find_free_port

_COOKIE_SECRET = b"i" * 32
_IDP_TOKEN_URL = "https://idp.example.test/token"
_IDP_USERINFO_URL = "https://idp.example.test/user"
_TEST_EMAIL = "ios-user@example.test"


@dataclass(frozen=True)
class OidcHandoffServer:
    """A live auth router used by the browser handoff test."""

    base_url: str


def _oidc_config(base_url: str) -> OIDCConfig:
    """Build a GitHub-shaped config whose browser endpoint is intercepted."""
    return OIDCConfig(
        issuer="https://idp.example.test",
        client_id="ios-e2e-client",
        client_secret="ios-e2e-secret",
        redirect_uri=f"{base_url}/auth/callback",
        cookie_secret=_COOKIE_SECRET,
        scopes="read:user user:email",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="github",
        authorization_endpoint=(
            f"{base_url.replace('127.0.0.1', 'localhost')}/test-idp/authorize"
        ),
        token_endpoint=_IDP_TOKEN_URL,
        jwks_uri=None,
        userinfo_endpoint=_IDP_USERINFO_URL,
        allow_invites=False,
    )


def _mock_idp_client() -> AsyncMock:
    """Return the async client used by the callback's token and email calls."""
    token_response = MagicMock()
    token_response.status_code = 200
    token_response.json.return_value = {
        "access_token": "ios-e2e-access-token",
        "token_type": "bearer",
    }
    token_response.text = "token response"

    email_response = MagicMock()
    email_response.status_code = 200
    email_response.json.return_value = [{"email": _TEST_EMAIL, "primary": True, "verified": True}]

    client = AsyncMock()
    client.post.return_value = token_response
    client.get.return_value = email_response

    context_manager = AsyncMock()
    context_manager.__aenter__.return_value = client
    context_manager.__aexit__.return_value = False
    return context_manager


def _build_auth_app(base_url: str, admin_list_path: Path) -> FastAPI:
    """Build the real OIDC auth router plus one protected browser page."""
    auth_provider = UnifiedAuthProvider(source="oidc", oidc_config=_oidc_config(base_url))
    app = FastAPI()
    app.include_router(
        create_auth_router(
            auth_provider=auth_provider,
            permission_store=None,
            admin_list=AdminList(admin_list_path),
        ),
        prefix="/auth",
    )

    @app.get("/test-idp/authorize")
    async def test_idp_authorize(request: Request) -> HTMLResponse:
        callback_url = request.query_params["redirect_uri"]
        callback_query = urlencode(
            {
                "code": "ios-e2e-authorization-code",
                "state": request.query_params["state"],
            }
        )
        continue_url = f"{callback_url}?{callback_query}"
        return HTMLResponse(
            "<html><body>"
            "<h1>Test identity provider</h1>"
            f'<a href="{html.escape(continue_url, quote=True)}">Continue as test user</a>'
            "</body></html>"
        )

    @app.get("/")
    async def protected_page(request: Request) -> HTMLResponse:
        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            return HTMLResponse("<h1>Authentication required</h1>", status_code=401)
        return HTMLResponse(f"<h1>Authenticated WebView</h1><p>{html.escape(user_id)}</p>")

    return app


def _wait_for_server(server: uvicorn.Server, base_url: str, timeout: float = 10.0) -> None:
    """Wait until the local uvicorn server reports that it has started."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started:
            return
        time.sleep(0.05)
    raise RuntimeError(f"OIDC handoff test server did not start at {base_url}")


@pytest.fixture
def oidc_handoff_server(tmp_path: Path) -> Iterator[OidcHandoffServer]:
    """Serve a deterministic production OIDC router for one browser test."""
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(
            _build_auth_app(base_url, tmp_path / "admins"),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    server_thread = threading.Thread(target=server.run, daemon=True)

    with patch.object(
        auth_routes.httpx,  # type: ignore[attr-defined]
        "AsyncClient",
        return_value=_mock_idp_client(),
    ):
        server_thread.start()
        _wait_for_server(server, base_url)
        try:
            yield OidcHandoffServer(base_url=base_url)
        finally:
            server.should_exit = True
            server_thread.join(timeout=10)
            if server_thread.is_alive():
                server.force_exit = True
                server_thread.join(timeout=5)


def test_system_browser_session_is_bridged_to_isolated_webview(
    browser: Browser,
    playwright: Playwright,
    oidc_handoff_server: OidcHandoffServer,
) -> None:
    """Complete browser OIDC, poll its ticket, and authenticate the WebView."""
    base_url = oidc_handoff_server.base_url
    native_request_context = playwright.request.new_context(base_url=base_url)
    system_browser_context = browser.new_context()
    webview_context = browser.new_context()
    try:
        ticket_response = native_request_context.post("/auth/cli-login")
        assert ticket_response.ok, ticket_response.text()
        ticket_payload = ticket_response.json()
        ticket = ticket_payload["ticket"]
        login_url = f"{base_url}{ticket_payload['login_url']}"

        pending_response = native_request_context.get(
            "/auth/cli-poll",
            params={"ticket": ticket},
        )
        assert pending_response.status == 202
        assert pending_response.json() == {"status": "pending"}

        system_browser_page = system_browser_context.new_page()
        system_browser_page.goto(login_url)
        expect(
            system_browser_page.get_by_role("heading", name="Test identity provider")
        ).to_be_visible()

        system_browser_page.get_by_role("link", name="Continue as test user").click()
        expect(system_browser_page.get_by_role("heading", name="Login successful")).to_be_visible()
        expect(system_browser_page.get_by_text(_TEST_EMAIL)).to_be_visible()

        poll_response = native_request_context.get(
            "/auth/cli-poll",
            params={"ticket": ticket},
        )
        assert poll_response.ok, poll_response.text()
        session_token = poll_response.json()["token"]

        webview_page = webview_context.new_page()
        unauthenticated_response = webview_page.goto(base_url)
        assert unauthenticated_response is not None
        assert unauthenticated_response.status == 401
        expect(webview_page.get_by_role("heading", name="Authentication required")).to_be_visible()

        webview_context.add_cookies(
            [{"name": "ap_session", "value": session_token, "url": base_url}]
        )
        webview_page.reload()
        expect(webview_page.get_by_role("heading", name="Authenticated WebView")).to_be_visible()
        expect(webview_page.get_by_text(_TEST_EMAIL)).to_be_visible()

        consumed_response = native_request_context.get(
            "/auth/cli-poll",
            params={"ticket": ticket},
        )
        assert consumed_response.status == 410
    finally:
        webview_context.close()
        system_browser_context.close()
        native_request_context.dispose()
