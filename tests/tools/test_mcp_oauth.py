"""Tests for generic MCP OAuth (browser sign-in + auto-refresh) glue.

Omnigent doesn't reimplement OAuth protocol logic — that lives in and is
tested by the ``mcp`` SDK's own :class:`~mcp.client.auth.oauth2.OAuthClientProvider`.
These tests cover only the glue this module supplies: token storage,
the local loopback callback listener, and provider construction.
"""

from __future__ import annotations

import io
from concurrent.futures import Future
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from omnigent.spec.types import MCPServerConfig
from omnigent.tools import mcp_oauth
from omnigent.tools.mcp_oauth import (
    OmnigentOAuthTokenStorage,
    _start_callback_server,
    build_oauth_client_provider,
)


@pytest.fixture(autouse=True)
def _file_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force the secret store's file backend at a tmp config home, off the
    real keychain — same isolation as tests/onboarding/test_secrets.py."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")


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


# ── Local loopback callback listener ─────────────────────────────────
#
# _OAuthCallbackHandler.do_GET is tested directly against a constructed
# instance (bypassing BaseHTTPRequestHandler.__init__, which otherwise
# demands a real connected socket) rather than over a real HTTP
# round-trip — deterministic, no threads/ports/timeouts involved, and
# exercises exactly the logic that matters: how a callback request maps
# to the result future.


def _make_handler(
    path: str,
) -> tuple[mcp_oauth._OAuthCallbackHandler, Future[tuple[str, str | None]]]:
    """Build a `_OAuthCallbackHandler` for `do_GET` unit tests.

    :param path: The request path + query string, e.g.
        ``"/callback?code=abc&state=xyz"``.
    :returns: ``(handler, result_future)`` with the handler's I/O and
        response-writing methods stubbed so `do_GET` can run without a
        real connection.
    """
    result_future: Future[tuple[str, str | None]] = Future()
    handler = object.__new__(mcp_oauth._OAuthCallbackHandler)
    handler.result_future = result_future
    handler.path = path
    handler.wfile = io.BytesIO()
    handler.send_response = lambda *a, **kw: None
    handler.send_header = lambda *a, **kw: None
    handler.end_headers = lambda: None
    return handler, result_future


class TestCallbackServer:
    def test_start_callback_server_binds_a_loopback_port(self) -> None:
        server, result_future = _start_callback_server()
        try:
            host, port = server.server_address
            assert host == "127.0.0.1"
            assert port > 0
        finally:
            result_future.cancel()
            server.server_close()

    def test_do_get_with_code_resolves_future(self) -> None:
        handler, result_future = _make_handler("/callback?code=abc123&state=xyz789")
        handler.do_GET()

        code, state = result_future.result(timeout=1)
        assert code == "abc123"
        assert state == "xyz789"

    def test_do_get_with_error_raises_in_future(self) -> None:
        handler, result_future = _make_handler("/callback?error=access_denied&state=xyz")
        handler.do_GET()

        with pytest.raises(RuntimeError, match="access_denied"):
            result_future.result(timeout=1)

    def test_do_get_missing_code_without_error_raises_in_future(self) -> None:
        handler, result_future = _make_handler("/callback?state=xyz")
        handler.do_GET()

        with pytest.raises(RuntimeError, match="No authorization code"):
            result_future.result(timeout=1)


class TestWithRedirectUriPort:
    def test_patches_the_redirect_uri_port(self) -> None:
        auth_url = (
            "https://auth.example.com/authorize"
            "?response_type=code&client_id=abc"
            "&redirect_uri=http%3A%2F%2F127.0.0.1%3A0%2Fcallback"
        )
        patched = mcp_oauth._with_redirect_uri_port(auth_url, 54321)

        params = parse_qs(urlparse(patched).query)
        assert params["redirect_uri"][0] == "http://127.0.0.1:54321/callback"
        # Everything else is untouched.
        assert params["client_id"][0] == "abc"

    def test_no_redirect_uri_param_is_a_no_op(self) -> None:
        auth_url = "https://auth.example.com/authorize?response_type=code"
        assert mcp_oauth._with_redirect_uri_port(auth_url, 54321) == auth_url


# ── build_oauth_client_provider ──────────────────────────────────────


class TestBuildOAuthClientProvider:
    def test_returns_none_when_oauth_not_set(self) -> None:
        config = MCPServerConfig(
            name="svc", transport="http", url="https://example.com/mcp", oauth=False
        )
        assert build_oauth_client_provider(config) is None

    def test_returns_provider_when_oauth_set(self) -> None:
        config = MCPServerConfig(
            name="svc", transport="http", url="https://example.com/mcp", oauth=True
        )
        provider = build_oauth_client_provider(config)
        assert provider is not None
        assert provider.context.server_url == "https://example.com/mcp"
        assert provider.context.client_metadata.client_name == "Omnigent"
        assert "127.0.0.1" in str(provider.context.client_metadata.redirect_uris[0])

    def test_raises_when_oauth_set_but_no_url(self) -> None:
        config = MCPServerConfig(name="svc", transport="http", url=None, oauth=True)
        with pytest.raises(RuntimeError, match="no url set"):
            build_oauth_client_provider(config)

    def test_each_call_builds_an_independent_provider(self) -> None:
        # Each connect/reconnect builds a fresh provider (mirrors
        # _resolve_http_headers' "resolve fresh each call" convention for
        # the Databricks token) — two providers for the same config must
        # not be the same object or silently share callback-listener state.
        # (redirect_uris is a fixed placeholder on both — the real
        # per-call loopback port isn't chosen until a sign-in actually
        # binds a listener; see _with_redirect_uri_port.)
        config = MCPServerConfig(
            name="svc", transport="http", url="https://example.com/mcp", oauth=True
        )
        provider_a = build_oauth_client_provider(config)
        provider_b = build_oauth_client_provider(config)
        assert provider_a is not None
        assert provider_b is not None
        assert provider_a is not provider_b
        assert provider_a.context is not provider_b.context

    async def test_stored_token_is_loaded_on_first_use(self) -> None:
        """A provider built for a URL that already has a stored token picks
        it up on first use, without needing a fresh browser sign-in."""
        import hashlib

        from mcp.shared.auth import OAuthToken

        url = "https://example.com/mcp-with-stored-token"
        server_key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        await OmnigentOAuthTokenStorage(server_key).set_tokens(
            OAuthToken(access_token="stored-tok")
        )

        config = MCPServerConfig(name="svc", transport="http", url=url, oauth=True)
        provider = build_oauth_client_provider(config)
        assert provider is not None

        await provider._initialize()
        assert provider.context.current_tokens is not None
        assert provider.context.current_tokens.access_token == "stored-tok"
