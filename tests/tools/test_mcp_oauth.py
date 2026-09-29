"""Unit tests for generic MCP OAuth (browser sign-in + auto-refresh) glue.

The OAuth protocol itself lives in the ``mcp`` SDK's
:class:`~mcp.client.auth.oauth2.OAuthClientProvider`. These tests cover what
Omnigent adds: token storage, the loopback callback listener, the
browser/headless handling and provider construction. The end-to-end flow
against a fake authorization server is in ``test_mcp_oauth_e2e.py``.
"""

from __future__ import annotations

import io
import time
from concurrent.futures import Future
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

from omnigent.spec.types import MCPServerConfig
from omnigent.tools import mcp_oauth
from omnigent.tools.mcp_oauth import (
    McpOAuthError,
    OmnigentOAuthTokenStorage,
    _CallbackListener,
    build_oauth_client_provider,
    find_oauth_error,
    oauth_server_key,
)


@pytest.fixture(autouse=True)
def _file_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force the secret store's file backend at a tmp config home, off the
    real keychain — same isolation as tests/onboarding/test_secrets.py."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")


def _oauth_config(url: str = "https://example.com/mcp") -> MCPServerConfig:
    return MCPServerConfig(name="svc", transport="http", url=url, oauth=True)


# ── OmnigentOAuthTokenStorage ────────────────────────────────────────


class TestOmnigentOAuthTokenStorage:
    async def test_get_tokens_returns_none_when_never_stored(self) -> None:
        storage = OmnigentOAuthTokenStorage("server-key-1")
        assert await storage.get_tokens() is None

    async def test_set_then_get_tokens_roundtrips(self) -> None:
        from mcp.shared.auth import OAuthToken

        storage = OmnigentOAuthTokenStorage("server-key-2")
        tokens = OAuthToken(access_token="tok_abc", refresh_token="rtok_xyz", expires_in=3600)
        await storage.set_tokens(tokens)

        loaded = await storage.get_tokens()
        assert loaded is not None
        assert loaded.access_token == "tok_abc"
        assert loaded.refresh_token == "rtok_xyz"

    async def test_set_then_get_client_info_roundtrips(self) -> None:
        from mcp.shared.auth import OAuthClientInformationFull

        storage = OmnigentOAuthTokenStorage("server-key-3")
        info = OAuthClientInformationFull(
            redirect_uris=["http://127.0.0.1:12345/callback"],  # type: ignore[arg-type]
            client_id="client-abc",
            client_secret="secret-xyz",
        )
        await storage.set_client_info(info)

        loaded = await storage.get_client_info()
        assert loaded is not None
        assert loaded.client_id == "client-abc"
        assert loaded.client_secret == "secret-xyz"

    async def test_two_server_keys_do_not_collide(self) -> None:
        from mcp.shared.auth import OAuthToken

        a = OmnigentOAuthTokenStorage("server-a")
        b = OmnigentOAuthTokenStorage("server-b")
        await a.set_tokens(OAuthToken(access_token="tok_a"))

        assert (await a.get_tokens()).access_token == "tok_a"  # type: ignore[union-attr]
        assert await b.get_tokens() is None

    async def test_corrupt_stored_tokens_degrade_to_none(self) -> None:
        from omnigent.onboarding.secrets import store_secret

        storage = OmnigentOAuthTokenStorage("server-corrupt")
        store_secret(mcp_oauth._tokens_secret_name("server-corrupt"), "not valid json")

        assert await storage.get_tokens() is None

    async def test_forget_deletes_both_secrets(self) -> None:
        from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

        storage = OmnigentOAuthTokenStorage("server-forget")
        await storage.set_tokens(OAuthToken(access_token="tok"))
        await storage.set_client_info(
            OAuthClientInformationFull(
                redirect_uris=["http://127.0.0.1:1/callback"],  # type: ignore[arg-type]
                client_id="cid",
            )
        )

        storage.forget()

        assert await storage.get_tokens() is None
        assert await storage.get_client_info() is None

    async def test_set_tokens_records_absolute_expiry(self) -> None:
        storage = OmnigentOAuthTokenStorage("server-expiry")
        before = time.time()
        await storage.set_tokens(OAuthToken(access_token="tok", expires_in=3600))

        reloaded = OmnigentOAuthTokenStorage("server-expiry")
        assert await reloaded.get_tokens() is not None
        assert reloaded.expires_at is not None
        assert before + 3600 <= reloaded.expires_at <= time.time() + 3600

    async def test_set_tokens_saves_the_attached_context_metadata(self) -> None:
        provider = build_oauth_client_provider(_oauth_config())
        assert provider is not None
        provider.context.oauth_metadata = OAuthMetadata.model_validate(
            {
                "issuer": "https://auth.example.com",
                "authorization_endpoint": "https://auth.example.com/authorize",
                "token_endpoint": "https://auth.example.com/oauth/token",
            }
        )
        await provider.context.storage.set_tokens(OAuthToken(access_token="tok"))

        reloaded = OmnigentOAuthTokenStorage(oauth_server_key("https://example.com/mcp"))
        await reloaded.get_tokens()
        assert reloaded.oauth_metadata is not None
        assert (
            str(reloaded.oauth_metadata.token_endpoint) == "https://auth.example.com/oauth/token"
        )


# ── Local loopback callback listener ─────────────────────────────────
#
# do_GET is exercised on a constructed handler (bypassing
# BaseHTTPRequestHandler.__init__, which needs a connected socket), so the
# request → result-future mapping is tested without threads or timeouts.


def _make_handler(
    path: str,
) -> tuple[mcp_oauth._OAuthCallbackHandler, Future[tuple[str, str | None]], io.BytesIO, list[int]]:
    """Build a `_OAuthCallbackHandler` for `do_GET` unit tests.

    :param path: The request path + query string, e.g.
        ``"/callback?code=abc&state=xyz"``.
    :returns: ``(handler, result_future, body, statuses)``: the response
        body written and the status codes sent are captured.
    """
    result_future: Future[tuple[str, str | None]] = Future()
    body = io.BytesIO()
    statuses: list[int] = []
    handler = object.__new__(mcp_oauth._OAuthCallbackHandler)
    handler.result_future = result_future
    handler.path = path
    handler.wfile = body
    handler.send_response = lambda code, *a, **kw: statuses.append(code)
    handler.send_error = lambda code, *a, **kw: statuses.append(code)
    handler.send_header = lambda *a, **kw: None
    handler.end_headers = lambda: None
    return handler, result_future, body, statuses


class TestCallbackHandler:
    def test_code_resolves_future(self) -> None:
        handler, result_future, _, _ = _make_handler("/callback?code=abc123&state=xyz789")
        handler.do_GET()

        assert result_future.result(timeout=1) == ("abc123", "xyz789")

    def test_error_raises_in_future(self) -> None:
        handler, result_future, _, _ = _make_handler("/callback?error=access_denied&state=xyz")
        handler.do_GET()

        with pytest.raises(McpOAuthError, match="access_denied"):
            result_future.result(timeout=1)

    def test_missing_code_without_error_raises_in_future(self) -> None:
        handler, result_future, _, _ = _make_handler("/callback?state=xyz")
        handler.do_GET()

        with pytest.raises(McpOAuthError, match="No authorization code"):
            result_future.result(timeout=1)

    def test_reflected_error_is_html_escaped(self) -> None:
        payload = "<script>alert(document.cookie)</script>"
        handler, _, body, _ = _make_handler(
            "/callback?error=" + payload + "&error_description=<img src=x onerror=alert(1)>"
        )
        handler.do_GET()

        page = body.getvalue().decode("utf-8")
        assert "<script>" not in page
        assert "<img" not in page
        assert "&lt;script&gt;alert(document.cookie)&lt;/script&gt;" in page
        assert "&lt;img src=x onerror=alert(1)&gt;" in page

    def test_other_paths_get_404_and_leave_the_future_pending(self) -> None:
        handler, result_future, _, statuses = _make_handler("/favicon.ico")
        handler.do_GET()

        assert statuses == [404]
        assert not result_future.done()


class TestCallbackListener:
    # Serving a real callback request is covered in test_mcp_oauth_e2e.py.

    def test_redirect_uri_names_the_bound_loopback_port(self) -> None:
        listener = _CallbackListener()
        try:
            assert listener.port > 0
            assert listener.redirect_uri == f"http://127.0.0.1:{listener.port}/callback"
        finally:
            listener.close()

    def test_reuses_the_preferred_port_when_free(self) -> None:
        first = _CallbackListener()
        port = first.port
        first.close()

        second = _CallbackListener(preferred_port=port)
        try:
            assert second.port == port
        finally:
            second.close()

    def test_falls_back_when_the_preferred_port_is_taken(self) -> None:
        busy = _CallbackListener()
        try:
            other = _CallbackListener(preferred_port=busy.port)
            try:
                assert other.port != busy.port
            finally:
                other.close()
        finally:
            busy.close()


# ── Browser / headless handling ──────────────────────────────────────


class TestBrowserHandling:
    def test_no_display_on_linux_means_no_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_oauth.sys, "platform", "linux")
        for var in ("BROWSER", "DISPLAY", "WAYLAND_DISPLAY"):
            monkeypatch.delenv(var, raising=False)
        opened: list[str] = []
        monkeypatch.setattr(mcp_oauth.webbrowser, "open", lambda url, **kw: opened.append(url))

        assert mcp_oauth._open_browser("https://auth.example.com/authorize") is False
        assert opened == []

    def test_display_means_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_oauth.sys, "platform", "linux")
        monkeypatch.delenv("BROWSER", raising=False)
        monkeypatch.setenv("DISPLAY", ":0")
        assert mcp_oauth._browser_available() is True

    async def test_redirect_fails_fast_when_no_browser(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_oauth, "_open_browser", lambda url: False)
        provider = build_oauth_client_provider(_oauth_config())
        assert provider is not None

        with pytest.raises(McpOAuthError, match="can't open a browser"):
            await provider._open_authorization_url("https://auth.example.com/authorize?x=1")

    async def test_callback_wait_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_oauth, "_CALLBACK_TIMEOUT_SECONDS", 0.05)
        provider = build_oauth_client_provider(_oauth_config())
        assert provider is not None
        listener = provider._open_callback_listener()
        try:
            with pytest.raises(McpOAuthError, match="Timed out"):
                await provider._wait_for_callback()
        finally:
            listener.close()

    def test_find_oauth_error_unwraps_exception_groups(self) -> None:
        inner = McpOAuthError("sign in first")
        # ExceptionGroup is a 3.11+ builtin; ruff's py310 target misflags it.
        nested = ExceptionGroup("n", [inner])  # noqa: F821
        group = ExceptionGroup("task group", [ValueError("other"), nested])  # noqa: F821
        assert find_oauth_error(group) is inner
        assert find_oauth_error(ValueError("x")) is None


# ── build_oauth_client_provider ──────────────────────────────────────


class TestBuildOAuthClientProvider:
    def test_returns_none_when_oauth_not_set(self) -> None:
        config = MCPServerConfig(
            name="svc", transport="http", url="https://example.com/mcp", oauth=False
        )
        assert build_oauth_client_provider(config) is None

    def test_returns_provider_when_oauth_set(self) -> None:
        provider = build_oauth_client_provider(_oauth_config())
        assert provider is not None
        assert provider.context.server_url == "https://example.com/mcp"
        assert provider.context.client_metadata.client_name == "Omnigent"

    def test_raises_when_oauth_set_but_no_url(self) -> None:
        config = MCPServerConfig(name="svc", transport="http", url=None, oauth=True)
        with pytest.raises(McpOAuthError, match="no url"):
            build_oauth_client_provider(config)

    @pytest.mark.parametrize(
        "url",
        ["http://example.com/mcp", "http://10.0.0.5/mcp", "ftp://example.com/mcp", "example.com"],
    )
    def test_rejects_non_https_non_loopback_urls(self, url: str) -> None:
        with pytest.raises(McpOAuthError, match="url"):
            build_oauth_client_provider(_oauth_config(url))

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/mcp",
            "http://localhost:8000/mcp",
            "http://127.0.0.1:8000/mcp",
            "http://127.1.2.3/mcp",
            "http://[::1]:8000/mcp",
        ],
    )
    def test_accepts_https_and_loopback_http(self, url: str) -> None:
        assert build_oauth_client_provider(_oauth_config(url)) is not None

    def test_each_call_builds_an_independent_provider(self) -> None:
        provider_a = build_oauth_client_provider(_oauth_config())
        provider_b = build_oauth_client_provider(_oauth_config())
        assert provider_a is not None
        assert provider_b is not None
        assert provider_a is not provider_b
        assert provider_a.context is not provider_b.context

    def test_listener_uri_becomes_the_redirect_uri(self) -> None:
        provider = build_oauth_client_provider(_oauth_config())
        assert provider is not None
        listener = provider._open_callback_listener()
        try:
            assert [str(u) for u in provider.context.client_metadata.redirect_uris or []] == [
                listener.redirect_uri
            ]
        finally:
            listener.close()

    async def test_listener_reuses_the_registered_port(self) -> None:
        free = _CallbackListener()
        port = free.port
        free.close()
        provider = build_oauth_client_provider(_oauth_config())
        assert provider is not None
        provider.context.client_info = OAuthClientInformationFull(
            redirect_uris=[f"http://127.0.0.1:{port}/callback"],  # type: ignore[list-item]
            client_id="cid",
        )
        listener = provider._open_callback_listener()
        try:
            assert listener.port == port
        finally:
            listener.close()

    async def test_stored_token_is_loaded_on_first_use(self) -> None:
        url = "https://example.com/mcp-with-stored-token"
        await OmnigentOAuthTokenStorage(oauth_server_key(url)).set_tokens(
            OAuthToken(access_token="stored-tok", expires_in=3600)
        )

        provider = build_oauth_client_provider(_oauth_config(url))
        assert provider is not None
        await provider._initialize()
        assert provider.context.current_tokens is not None
        assert provider.context.current_tokens.access_token == "stored-tok"
        assert provider.context.is_token_valid()

    async def test_expired_stored_token_is_refreshable_not_valid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A new provider must see a stored token's expiry, so it refreshes
        up front instead of sending the stale token and signing in again."""
        url = "https://example.com/mcp-expired"
        storage = OmnigentOAuthTokenStorage(oauth_server_key(url))
        await storage.set_tokens(OAuthToken(access_token="old", refresh_token="rt", expires_in=60))
        await storage.set_client_info(
            OAuthClientInformationFull(
                redirect_uris=["http://127.0.0.1:1/callback"],  # type: ignore[list-item]
                client_id="cid",
            )
        )
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + 120)

        provider = build_oauth_client_provider(_oauth_config(url))
        assert provider is not None
        await provider._initialize()
        assert not provider.context.is_token_valid()
        assert provider.context.can_refresh_token()
