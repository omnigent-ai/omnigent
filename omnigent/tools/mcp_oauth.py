"""Generic MCP OAuth — browser sign-in with PKCE and automatic refresh.

Builds on the MCP SDK's :class:`mcp.client.auth.oauth2.OAuthClientProvider`
(RFC 8414/9728 discovery, RFC 7591 dynamic client registration,
authorization_code + PKCE). Omnigent supplies what the SDK leaves to the
host application:

- a :class:`TokenStorage` backed by the OS-keychain-or-file secret store
  (:mod:`omnigent.onboarding.secrets`), which also remembers when the access
  token expires and where the token endpoint is, so a new connection (or a
  new process) refreshes an expired token instead of asking for a new
  browser sign-in;
- a loopback HTTP listener for the authorization redirect, bound before the
  flow needs a redirect URI, so client registration, the authorization
  request and the token exchange all carry the same ``redirect_uri`` (the
  client registers again if its registered port is taken);
- a refresh attempt when the server rejects a token with 401, before
  falling back to a browser sign-in.

Sign-in is local-only: the browser must run on the machine running this
code. Where no browser can be opened (a remote or headless server) the
connection fails fast with :class:`McpOAuthError` instead of waiting for a
callback that can never arrive.

Used when an ``MCPServerConfig`` sets ``oauth=True`` (``auth: {type:
oauth}`` in YAML); :func:`build_oauth_client_provider` is the entry point
:mod:`omnigent.tools.mcp` calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import sys
import threading
import time
import webbrowser
from collections.abc import AsyncGenerator
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from mcp.client.auth.oauth2 import OAuthClientProvider, OAuthContext
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from pydantic import AnyUrl, BaseModel

from omnigent.onboarding.secrets import delete_secret, load_secret, store_secret
from omnigent.spec.types import mcp_oauth_url_problem

if TYPE_CHECKING:
    from httpx import Request, Response

    from omnigent.spec.types import MCPServerConfig

_logger = logging.getLogger(__name__)

# How long to wait for the user to finish signing in in the browser.
_CALLBACK_TIMEOUT_SECONDS = 300.0

_CALLBACK_HOST = "127.0.0.1"
_CALLBACK_PATH = "/callback"

_SECRET_NAME_PREFIX = "mcp-oauth"


class McpOAuthError(RuntimeError):
    """An MCP OAuth sign-in problem the user has to act on.

    Its message is written for the user: it becomes the MCP server's
    connection error.
    """


def _tokens_secret_name(server_key: str) -> str:
    return f"{_SECRET_NAME_PREFIX}:tokens:{server_key}"


def _client_info_secret_name(server_key: str) -> str:
    return f"{_SECRET_NAME_PREFIX}:client:{server_key}"


class _TokenRecord(BaseModel):
    """What is persisted per server: the token set plus what refresh needs.

    ``expires_at`` is absolute (epoch seconds) because the token's own
    ``expires_in`` is relative to when it was issued. The discovered
    metadata tells a later connection which token endpoint to refresh
    against without first taking a 401.
    """

    tokens: OAuthToken
    expires_at: float | None = None
    oauth_metadata: OAuthMetadata | None = None
    protected_resource_metadata: ProtectedResourceMetadata | None = None


class OmnigentOAuthTokenStorage:
    """:class:`mcp.client.auth.oauth2.TokenStorage` backed by the OS keychain.

    Persists to the same store as provider API keys
    (:mod:`omnigent.onboarding.secrets` — OS keychain, falling back to a
    ``0600`` JSON file), keyed by *server_key* so two MCP servers never
    collide and a URL change gets a fresh credential.

    Besides the SDK protocol it keeps the loaded token's absolute expiry
    and discovered metadata (:attr:`expires_at`, :attr:`oauth_metadata`,
    :attr:`protected_resource_metadata`) for
    :class:`OmnigentOAuthClientProvider` to restore.

    :param server_key: Stable identifier for the MCP server, e.g. a
        digest of its URL (see :func:`build_oauth_client_provider`).
    """

    def __init__(self, server_key: str) -> None:
        self._tokens_name = _tokens_secret_name(server_key)
        self._client_info_name = _client_info_secret_name(server_key)
        self._context: OAuthContext | None = None
        self.expires_at: float | None = None
        self.oauth_metadata: OAuthMetadata | None = None
        self.protected_resource_metadata: ProtectedResourceMetadata | None = None

    def attach(self, context: OAuthContext) -> None:
        """Save *context*'s discovered metadata alongside future tokens."""
        self._context = context

    async def get_tokens(self) -> OAuthToken | None:
        """Return the stored token set, or ``None`` if never authenticated."""
        raw = load_secret(self._tokens_name)
        if raw is None:
            return None
        try:
            record = _TokenRecord.model_validate_json(raw)
        except ValueError:
            # A corrupt stored value degrades to a fresh sign-in, not a crash.
            _logger.warning(
                "Discarding unreadable stored MCP OAuth tokens for %s", self._tokens_name
            )
            return None
        self.expires_at = record.expires_at
        self.oauth_metadata = record.oauth_metadata
        self.protected_resource_metadata = record.protected_resource_metadata
        return record.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Persist the token set, e.g. after a fresh grant or a refresh."""
        self.expires_at = time.time() + tokens.expires_in if tokens.expires_in else None
        if self._context is not None:
            self.oauth_metadata = self._context.oauth_metadata
            self.protected_resource_metadata = self._context.protected_resource_metadata
        record = _TokenRecord(
            tokens=tokens,
            expires_at=self.expires_at,
            oauth_metadata=self.oauth_metadata,
            protected_resource_metadata=self.protected_resource_metadata,
        )
        store_secret(self._tokens_name, record.model_dump_json(exclude_none=True))

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
        """Delete both stored secrets, e.g. to sign out of a server.

        Not part of the SDK's ``TokenStorage`` protocol.
        """
        delete_secret(self._tokens_name)
        delete_secret(self._client_info_name)


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Captures ``?code=&state=`` from the authorization redirect.

    ``result_future`` is set as a class attribute by
    :class:`_CallbackListener`, since ``HTTPServer`` instantiates this class
    itself. Only the first callback settles the future.
    """

    result_future: Future[tuple[str, str | None]]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != _CALLBACK_PATH:
            # e.g. the browser's /favicon.ico probe
            self.send_error(404)
            return
        params = parse_qs(parsed.query)
        error = params.get("error", [None])[0]
        description = params.get("error_description", [None])[0]
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        if error or not code:
            reason = error or "No authorization code received."
            if description:
                reason = f"{reason}: {description}"
            # Every reflected value comes from the redirecting server: escape it.
            self._respond("Sign-in failed", html.escape(reason))
            if not self.result_future.done():
                self.result_future.set_exception(
                    McpOAuthError(f"MCP OAuth sign-in failed: {reason}")
                )
            return
        self._respond("Signed in", "You can close this tab and return to Omnigent.")
        if not self.result_future.done():
            self.result_future.set_result((code, state))

    def _respond(self, title: str, message_html: str) -> None:
        body = f"<html><body><h3>{title}</h3><p>{message_html}</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        # One-shot local listener; the default stderr access log is noise.
        pass


class _CallbackListener:
    """A loopback HTTP listener that receives one OAuth redirect.

    :param preferred_port: Port to try first, e.g. the one in the
        registered redirect URI; falls back to an OS-assigned port when it
        is ``None`` or already taken.
    """

    def __init__(self, preferred_port: int | None = None) -> None:
        self.result: Future[tuple[str, str | None]] = Future()
        handler_cls = type(
            "_BoundOAuthCallbackHandler",
            (_OAuthCallbackHandler,),
            {"result_future": self.result},
        )
        self._server = _bind_http_server(handler_cls, preferred_port)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="omnigent-mcp-oauth-callback",
            daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://{_CALLBACK_HOST}:{self.port}{_CALLBACK_PATH}"

    def close(self) -> None:
        """Stop serving and release the port (blocks up to one poll interval)."""
        self._server.shutdown()
        self._server.server_close()
        if not self.result.done():
            self.result.cancel()


def _bind_http_server(
    handler_cls: type[BaseHTTPRequestHandler], preferred_port: int | None
) -> HTTPServer:
    if preferred_port:
        try:
            return HTTPServer((_CALLBACK_HOST, preferred_port), handler_cls)
        except OSError:
            _logger.debug("MCP OAuth callback port %d is busy; using another", preferred_port)
    return HTTPServer((_CALLBACK_HOST, 0), handler_cls)


def _registered_loopback_port(client_info: OAuthClientInformationFull | None) -> int | None:
    """The port of a stored client's registered loopback redirect URI, if any.

    Reusing it keeps the redirect URI identical to the registered one, which
    servers that don't allow loopback port variance (RFC 8252 §7.3) require.
    """
    if client_info is None or not client_info.redirect_uris:
        return None
    registered = urlparse(str(client_info.redirect_uris[0]))
    if registered.hostname != _CALLBACK_HOST or registered.path != _CALLBACK_PATH:
        return None
    return registered.port


def _busy_port_message(server_name: str, port: int | None) -> str:
    port_name = f"port {port}" if port else "the port"
    return (
        f"MCP server {server_name!r} asks you to sign in again for more access, but "
        f"{port_name} its sign-in redirect is registered on is in use by another "
        f"program. Free {port_name} and reconnect."
    )


def _browser_available() -> bool:
    """Whether this process can show a browser to the person using it.

    On Linux and other X11/Wayland systems a browser needs a display; without
    one, :func:`webbrowser.open` may start a console browser nobody can see.
    An explicit ``BROWSER`` setting is trusted.
    """
    if os.environ.get("BROWSER"):
        return True
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _open_browser(url: str) -> bool:
    """Open *url* in the user's browser; ``False`` if that isn't possible."""
    if not _browser_available():
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except webbrowser.Error:
        return False


class OmnigentOAuthClientProvider(OAuthClientProvider):
    """The SDK's OAuth provider, adjusted for loopback sign-in and refresh.

    Adds three behaviours on top of :class:`OAuthClientProvider`:

    - Restores the stored token's expiry and discovered metadata on first
      use, so an expired token is refreshed (at the right token endpoint)
      rather than sent and rejected.
    - On a 401, tries the stored refresh token before a browser sign-in.
    - Binds the loopback callback listener as soon as a sign-in is
      unavoidable and sets its URI in the client metadata, which the SDK
      then uses for registration, authorization and token exchange alike.

    :param server_name: The MCP server's configured name, for messages.
    :param server_url: The MCP server URL.
    :param storage: Token storage for this server.
    """

    def __init__(
        self,
        server_name: str,
        server_url: str,
        storage: OmnigentOAuthTokenStorage,
    ) -> None:
        client_metadata = OAuthClientMetadata(
            # Replaced by the bound listener's URI before the SDK reads it.
            redirect_uris=[AnyUrl(f"http://{_CALLBACK_HOST}{_CALLBACK_PATH}")],
            client_name="Omnigent",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
        super().__init__(
            server_url=server_url,
            client_metadata=client_metadata,
            storage=storage,
            redirect_handler=self._open_authorization_url,
            callback_handler=self._wait_for_callback,
            timeout=_CALLBACK_TIMEOUT_SECONDS,
        )
        self._server_name = server_name
        self._storage = storage
        self._listener: _CallbackListener | None = None
        storage.attach(self.context)

    async def _initialize(self) -> None:
        await super()._initialize()
        # The SDK loads the tokens but not when they expire, so it would
        # treat an expired token as valid and never refresh it up front.
        self.context.token_expiry_time = self._storage.expires_at
        if self.context.oauth_metadata is None:
            self.context.oauth_metadata = self._storage.oauth_metadata
        if self.context.protected_resource_metadata is None:
            self.context.protected_resource_metadata = self._storage.protected_resource_metadata

    async def _handle_refresh_response(self, response: Response) -> bool:
        previous = self.context.current_tokens
        previous_refresh_token = previous.refresh_token if previous else None
        refreshed = await super()._handle_refresh_response(response)
        tokens = self.context.current_tokens
        if (
            refreshed
            and tokens is not None
            and not tokens.refresh_token
            and previous_refresh_token
        ):
            # RFC 6749 §6: a refresh response may omit refresh_token, in which
            # case the one just used stays valid; keep it for the next refresh.
            self.context.current_tokens = tokens.model_copy(
                update={"refresh_token": previous_refresh_token}
            )
            await self._storage.set_tokens(self.context.current_tokens)
        return refreshed

    async def async_auth_flow(self, request: Request) -> AsyncGenerator[Request, Response]:
        """Run the SDK's flow, adding refresh-on-401 and the callback listener.

        Relays every request and response between httpx and the SDK's
        generator; only the first response to *request* itself is acted on.
        """
        flow = super().async_auth_flow(request)
        listener: _CallbackListener | None = None
        handled_first_response = False
        try:
            outgoing = await flow.__anext__()
            while True:
                response = yield outgoing
                if outgoing is request and not handled_first_response:
                    handled_first_response = True
                    if response.status_code == 401 and self.context.can_refresh_token():
                        refresh_response = yield await self._refresh_token()
                        if await self._handle_refresh_response(refresh_response):
                            self._add_auth_header(request)
                            response = yield request
                    if response.status_code in (401, 403):
                        # The SDK signs in again next (a 403 may ask for more scope).
                        listener = self._open_callback_listener(
                            can_register=response.status_code == 401
                        )
                try:
                    outgoing = await flow.asend(response)
                except StopAsyncIteration:
                    return
        finally:
            await flow.aclose()
            if listener is not None:
                self._listener = None
                # Synchronous so cancellation can't skip it; blocks ≤ one poll.
                listener.close()

    def _open_callback_listener(self, *, can_register: bool) -> _CallbackListener:
        """Bind the callback listener, preferably on the registered port.

        If that port is taken, the redirect URI changes, which a server that
        matches redirect URIs exactly rejects. A full sign-in (*can_register*)
        then registers the client again with the new URI; a scope step-up,
        which the SDK runs without registration, fails with a clear error.

        :param can_register: Whether the SDK registers a client before
            authorizing, i.e. this is a full sign-in rather than a step-up.
        :raises McpOAuthError: If the registered port is busy and the client
            can't be registered again.
        """
        client_info = self.context.client_info
        registered_port = _registered_loopback_port(client_info)
        listener = _CallbackListener(registered_port)
        if client_info is not None and listener.port != registered_port:
            if not can_register:
                listener.close()
                raise McpOAuthError(_busy_port_message(self._server_name, registered_port))
            _logger.info(
                "MCP server %r: sign-in redirect port %s is unavailable; registering again "
                "with port %d",
                self._server_name,
                registered_port,
                listener.port,
            )
            # Stored client info only ever comes from dynamic registration here.
            self.context.client_info = None
        self._listener = listener
        self.context.client_metadata.redirect_uris = [AnyUrl(listener.redirect_uri)]
        return listener

    async def _open_authorization_url(self, authorization_url: str) -> None:
        if not _open_browser(authorization_url):
            raise McpOAuthError(
                f"MCP server {self._server_name!r} needs a browser sign-in, but Omnigent "
                "can't open a browser where it is running (for example a remote or "
                "headless server). OAuth sign-in currently needs Omnigent and your browser "
                "on the same machine: run the agent locally, or replace "
                "'auth: {type: oauth}' with a static 'Authorization' header for this server."
            )
        _logger.info("MCP server %r requires sign-in; opened the browser", self._server_name)
        print(
            f"\nSign in to connect the '{self._server_name}' MCP server. "
            f"If your browser didn't open, visit:\n  {authorization_url}\n"
        )

    async def _wait_for_callback(self) -> tuple[str, str | None]:
        listener = self._listener
        if listener is None:
            raise McpOAuthError(
                f"MCP server {self._server_name!r}: no sign-in callback listener is running"
            )
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(listener.result), timeout=_CALLBACK_TIMEOUT_SECONDS
            )
        except TimeoutError:
            raise McpOAuthError(
                f"Timed out after {int(_CALLBACK_TIMEOUT_SECONDS // 60)} minutes waiting for "
                f"the browser sign-in to MCP server {self._server_name!r}; reconnect to try again."
            ) from None


def find_oauth_error(exc: BaseException) -> McpOAuthError | None:
    """Return the :class:`McpOAuthError` behind *exc*, if there is one.

    The MCP transports run requests in task groups, so a sign-in failure
    reaches the caller wrapped in an exception group.
    """
    if isinstance(exc, McpOAuthError):
        return exc
    # A 3.11+ builtin (we require 3.12); ruff's py310 target misflags it.
    if isinstance(exc, BaseExceptionGroup):  # noqa: F821
        for inner in exc.exceptions:
            found = find_oauth_error(inner)
            if found is not None:
                return found
    return None


def oauth_server_key(url: str) -> str:
    """Storage key for *url*'s tokens: stable for as long as the URL is."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def build_oauth_client_provider(config: MCPServerConfig) -> OmnigentOAuthClientProvider | None:
    """Build an OAuth provider for *config*, or ``None`` if OAuth is off.

    Callers can pass the result straight to ``streamablehttp_client`` /
    ``sse_client``'s ``auth=`` argument. Building one per connection is
    cheap: tokens, expiry and discovered metadata are loaded from storage
    on first use.

    :param config: The MCP server config.
    :returns: A provider when ``config.oauth`` is set, else ``None``.
    :raises McpOAuthError: If OAuth is on but the URL is missing or isn't
        https (or loopback http).
    """
    if not config.oauth:
        return None
    if config.url is None:
        raise McpOAuthError(f"MCP server {config.name!r} has auth type 'oauth' but no url")
    problem = mcp_oauth_url_problem(config.url)
    if problem is not None:
        raise McpOAuthError(f"MCP server {config.name!r} auth type 'oauth': the url {problem}")
    storage = OmnigentOAuthTokenStorage(oauth_server_key(config.url))
    return OmnigentOAuthClientProvider(config.name, config.url, storage)
