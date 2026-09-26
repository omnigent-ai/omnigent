"""Generic MCP OAuth — browser sign-in with PKCE and automatic refresh.

Wires the MCP SDK's own :class:`mcp.client.auth.oauth2.OAuthClientProvider`
(RFC 8414/9728 discovery, RFC 7591 dynamic client registration,
authorization_code + PKCE, and transparent 401 re-auth/refresh) into
Omnigent: a :class:`TokenStorage` backed by the existing OS-keychain-or-file
secret store (:mod:`omnigent.onboarding.secrets`), and a local loopback HTTP
listener for the authorization redirect. Omnigent does not reimplement any
OAuth protocol logic — that all lives in the SDK and is exercised by its own
test suite; this module only supplies the storage/redirect/callback glue the
SDK asks for.

Used when an ``MCPServerConfig`` sets ``oauth=True`` (``auth: {type:
oauth}`` in YAML). See :func:`build_oauth_client_provider`, the single
entry point :mod:`omnigent.tools.mcp` calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import webbrowser
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from omnigent.onboarding.secrets import delete_secret, load_secret, store_secret

if TYPE_CHECKING:
    from omnigent.spec.types import MCPServerConfig

_logger = logging.getLogger(__name__)

# Local loopback callback listener never waits longer than the SDK's own
# authorization-flow timeout (OAuthClientProvider default: 300s) — bounding
# it independently would just produce a confusing second timeout.
_CALLBACK_TIMEOUT_SECONDS = 300.0

_SECRET_NAME_PREFIX = "mcp-oauth"


def _tokens_secret_name(server_key: str) -> str:
    return f"{_SECRET_NAME_PREFIX}:tokens:{server_key}"


def _client_info_secret_name(server_key: str) -> str:
    return f"{_SECRET_NAME_PREFIX}:client:{server_key}"


class OmnigentOAuthTokenStorage:
    """:class:`mcp.client.auth.oauth2.TokenStorage` backed by the OS keychain.

    Persists to the same store as provider API keys
    (:mod:`omnigent.onboarding.secrets` — OS keychain, falling back to a
    ``0600`` JSON file), keyed by *server_key* so two MCP servers never
    collide and a URL change gets a fresh credential rather than reusing
    a stale one.

    :param server_key: Stable identifier for the MCP server this storage
        instance authenticates — :func:`build_oauth_client_provider`
        derives this from the server's URL.
    """

    def __init__(self, server_key: str) -> None:
        self._tokens_name = _tokens_secret_name(server_key)
        self._client_info_name = _client_info_secret_name(server_key)

    async def get_tokens(self) -> OAuthToken | None:
        """Return the stored token set, or ``None`` if never authenticated."""
        raw = load_secret(self._tokens_name)
        if raw is None:
            return None
        try:
            return OAuthToken.model_validate_json(raw)
        except ValueError:
            # Corrupt/unrecognized stored value — treat as absent rather
            # than raising, so a bad on-disk secret degrades to a fresh
            # sign-in instead of a hard crash.
            _logger.warning(
                "Discarding unreadable stored MCP OAuth tokens for %s", self._tokens_name
            )
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Persist the token set, e.g. after a fresh grant or a refresh."""
        store_secret(self._tokens_name, tokens.model_dump_json())

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """Return the stored dynamic-client-registration info, if any."""
        raw = load_secret(self._client_info_name)
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(raw)
        except ValueError:
            _logger.warning(
                "Discarding unreadable stored MCP OAuth client info for %s", self._client_info_name
            )
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        """Persist client registration info returned by the auth server."""
        store_secret(self._client_info_name, client_info.model_dump_json())

    def forget(self) -> None:
        """Delete both stored secrets — used when a config drops ``oauth``.

        Not part of the SDK's ``TokenStorage`` protocol; called directly by
        callers that want to revoke a stored grant (e.g. a future ``omnigent
        mcp logout`` command).
        """
        delete_secret(self._tokens_name)
        delete_secret(self._client_info_name)


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """One-shot handler: captures ``?code=&state=`` from the redirect, then
    lets the server shut down. Set as class attributes by
    :func:`_start_callback_server` since ``HTTPServer`` instantiates this
    class itself.
    """

    result_future: Future[tuple[str, str | None]]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        error = params.get("error", [None])[0]
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if error or not code:
            reason = error or "No authorization code received."
            body = f"<html><body><h3>Sign-in failed</h3><p>{reason}</p></body></html>"
            self.wfile.write(body.encode("utf-8"))
            if not self.result_future.done():
                self.result_future.set_exception(
                    RuntimeError(f"MCP OAuth authorization failed: {reason}")
                )
        else:
            body = (
                "<html><body><h3>Signed in</h3>"
                "<p>You can close this tab and return to Omnigent.</p></body></html>"
            )
            self.wfile.write(body.encode("utf-8"))
            if not self.result_future.done():
                self.result_future.set_result((code, state))

    def log_message(self, format: str, *args: object) -> None:
        # Silence BaseHTTPRequestHandler's default stderr access log —
        # this is a one-shot local listener, not a service worth logging.
        pass


def _start_callback_server() -> tuple[HTTPServer, Future[tuple[str, str | None]]]:
    """Bind an ephemeral loopback listener for the OAuth redirect.

    Binds on an OS-assigned port (``0``) — the real port is only known
    after this returns, via ``server.server_address[1]``. Callers that
    already promised a specific ``redirect_uri`` to the auth server (e.g.
    in a dynamic client registration request) need to patch that port in
    after the fact; see :func:`_with_redirect_uri_port` in
    :func:`build_oauth_client_provider`.

    :returns: ``(server, result_future)`` — *result_future* resolves with
        ``(code, state)`` once the browser hits the callback, or raises
        if the auth server reported an error.
    """
    result_future: Future[tuple[str, str | None]] = Future()
    handler_cls = type(
        "_BoundOAuthCallbackHandler",
        (_OAuthCallbackHandler,),
        {"result_future": result_future},
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(
        target=server.handle_request, name="omnigent-mcp-oauth-callback", daemon=True
    )
    thread.start()
    return server, result_future


def _with_redirect_uri_port(authorization_url: str, port: int) -> str:
    """Patch the ``redirect_uri`` query param's port in an authorization URL.

    The client is registered with a placeholder ``redirect_uris`` entry
    (the real loopback port isn't known until the callback listener
    actually binds, which happens lazily — see
    :func:`build_oauth_client_provider`), so the authorization request the
    SDK builds initially carries that placeholder. RFC 8252 §7.3 expects
    exactly this: authorization servers supporting native/loopback clients
    must tolerate the redirect URI's port varying from what was
    registered, since a native app cannot know it in advance.

    :param authorization_url: The full authorization URL the SDK built.
    :param port: The actual bound loopback port.
    :returns: *authorization_url* with its ``redirect_uri`` param's port
        replaced by *port*; unchanged if there was no ``redirect_uri`` param.
    """
    parsed = urlparse(authorization_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    redirect_uris = params.get("redirect_uri")
    if not redirect_uris:
        return authorization_url
    redirect_parsed = urlparse(redirect_uris[0])
    patched_redirect = redirect_parsed._replace(netloc=f"{redirect_parsed.hostname}:{port}")
    params["redirect_uri"] = [urlunparse(patched_redirect)]
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


def build_oauth_client_provider(config: MCPServerConfig) -> OAuthClientProvider | None:
    """Build an :class:`OAuthClientProvider` for *config*, or ``None``.

    Returns ``None`` when ``config.oauth`` is not set, so callers can
    unconditionally do ``auth = build_oauth_client_provider(config)`` and
    pass the result straight into ``streamablehttp_client``/``sse_client``'s
    ``auth=`` kwarg.

    The returned provider handles everything from here: on first use it
    discovers the auth server (RFC 8414/9728), registers a dynamic client
    (RFC 7591) if needed, opens the user's browser for authorization_code +
    PKCE consent, and stores the resulting tokens via
    :class:`OmnigentOAuthTokenStorage`. On subsequent connections it reuses
    the stored tokens and refreshes them transparently (including on a
    live 401), so most runs never touch the browser at all.

    :param config: The MCP server config declaring ``oauth: True``.
    :returns: A configured provider, or ``None`` if OAuth isn't enabled
        for this server.
    """
    if not config.oauth:
        return None
    if config.url is None:
        raise RuntimeError(f"MCP server {config.name!r} has oauth=True but no url set")

    # Keyed by the server URL alone (not the full connection-pooling hash
    # `omnigent.runner.mcp_manager.compute_server_hash` uses, which this
    # module can't import without a circular dependency) — token identity
    # only needs to be stable across reconnects to the same real endpoint;
    # it doesn't need to change when unrelated fields like `timeout` do.
    server_key = hashlib.sha256(config.url.encode("utf-8")).hexdigest()[:16]
    storage = OmnigentOAuthTokenStorage(server_key)

    # The listener binds lazily, inside `redirect_handler` — which
    # `OAuthClientProvider` only calls when a fresh browser sign-in is
    # actually needed (no valid stored token to reuse or refresh). Binding
    # a real socket + thread on every connection attempt, most of which
    # never need one, would leak a listener each time the stored token
    # was simply reused. `_pending` is how `callback_handler` learns what
    # `redirect_handler` bound, since the SDK calls them as two separate
    # awaits rather than passing state between them itself.
    _pending: dict[str, tuple[HTTPServer, Future[tuple[str, str | None]]]] = {}

    async def redirect_handler(authorization_url: str) -> None:
        server, result_future = _start_callback_server()
        _pending["server"] = (server, result_future)
        port = server.server_address[1]
        # The client was registered with a placeholder redirect_uri (the
        # real port wasn't known yet); patch it into the actual
        # authorization request now that the listener is bound. Loopback
        # port variance is expected by spec — see _with_redirect_uri_port.
        authorization_url = _with_redirect_uri_port(authorization_url, port)
        _logger.info(
            "MCP server %r requires sign-in — opening browser: %s", config.name, authorization_url
        )
        print(f"\nSign in to connect the '{config.name}' MCP server:\n  {authorization_url}\n")
        # Best-effort: webbrowser.open() safely returns False (never raises)
        # when no display/browser is available, e.g. a headless runner —
        # the printed URL above is the fallback for that case.
        webbrowser.open(authorization_url)

    async def callback_handler() -> tuple[str, str | None]:
        server, result_future = _pending["server"]
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(result_future), timeout=_CALLBACK_TIMEOUT_SECONDS
            )
        finally:
            server.server_close()

    client_metadata = OAuthClientMetadata(
        # Placeholder — the real loopback port isn't known until
        # redirect_handler binds the listener (patched in there via
        # _with_redirect_uri_port). Pydantic just needs a syntactically
        # valid entry to build the initial authorization request.
        redirect_uris=["http://127.0.0.1:0/callback"],  # type: ignore[arg-type]
        client_name="Omnigent",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )

    return OAuthClientProvider(
        server_url=config.url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=_CALLBACK_TIMEOUT_SECONDS,
    )
