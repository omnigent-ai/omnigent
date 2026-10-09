"""E2E regression tests: runner authentication must recover after a delayed re-login.

Reported journey: a previously connected runner's
credentials expire, the server starts rejecting its bearer (HTTP 401/403 or a
Databricks Apps OAuth login redirect), and the user's re-login takes several
minutes. The runner must survive that window and recover on its own once
credentials return — instead it bricks until process restart. Synchronous
credential providers also execute on the asyncio event loop during HTTP
authentication.

Four facets, each driven through the runner's real outbound auth code over
real sockets (no transports are stubbed), with synthetic credential providers
and a local server standing in for the Databricks Apps front door — the same
controlled setup the report used:

1. ``_InitialAuthTokenFactory``: after the bootstrap bearer is invalidated
   while credential discovery returns ``None`` (login still in progress), a
   later successful login must be rediscovered (a short, bounded rediscovery
   backoff is fine; requiring a process restart is not). The wrapper
   permanently caches the missing fallback factory and returns ``None``
   forever.
2. ``serve_tunnel``: an ESTABLISHED tunnel whose reconnect bounces to the
   OAuth login page while renewal is unavailable must keep retrying and
   reconnect once login completes. The immediate-refresh path raises a fatal
   ``RuntimeError`` instead, killing the tunnel task.
3. ``_RunnerDatabricksAuth`` on ``httpx.AsyncClient``: the synchronous
   credential provider must not execute on the running event loop (it can
   shell out to the Databricks CLI or block on HTTP for seconds).
4. After the bootstrap fallback resolves to the managed-mint provider, a
   server rejection of the minted credential must reach that provider
   (invalidate + re-mint). ``_InitialAuthTokenFactory.invalidate()`` returns
   ``False`` without forwarding, so every retry re-sends the same rejected
   token and callbacks 401 forever.

Every assertion states the EXPECTED (fixed) behaviour, so each test fails on
the live bug and passes once the fix lands. No real credentials are touched:
the ``_isolated_credentials`` fixture scrubs Databricks/runner auth state into
a temp directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import http
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.server import serve

import omnigent.runner._entry as runner_entry
from omnigent.runner._entry import (
    _InitialAuthTokenFactory,
    _RunnerDatabricksAuth,
)
from omnigent.runner.identity import (
    RUNNER_DELEGATED_AUTH_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
)
from omnigent.runner.transports.ws_tunnel.serve import serve_tunnel
from tests._helpers.live_server import find_free_port

_BOOTSTRAP_BEARER = "expired-host-bootstrap-bearer"
_FRESH_OIDC_TOKEN = "fresh-oidc-token-after-relogin"
_OAUTH_LOGIN_URL = (
    "https://workspace.example.invalid/oidc/oauth2/v2.0/authorize"
    "?redirect_uri=https%3A%2F%2Fworkspace.example.invalid%2F.auth%2Fcallback"
)


@pytest.fixture()
def isolated_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate every credential-discovery source into a temp directory.

    Scrubs ambient Databricks and runner-auth environment variables, points
    the Omnigent token store (``auth_tokens.json``) at a temp state dir, and
    points the Databricks SDK at an empty config file — so credential
    discovery genuinely returns ``None`` until a test writes a stored token
    (simulating the user completing ``omnigent login`` minutes later).

    :param tmp_path: pytest-provided temp directory.
    :param monkeypatch: pytest monkeypatch fixture.
    :yields: The isolated Omnigent state directory holding
        ``auth_tokens.json``.
    """
    state_dir = tmp_path / "omnigent-state"
    state_dir.mkdir()
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(state_dir))
    import os as _os

    for name in list(_os.environ):
        if name.startswith("DATABRICKS_"):
            monkeypatch.delenv(name, raising=False)
    empty_cfg = tmp_path / "empty-databrickscfg"
    empty_cfg.write_text("")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(empty_cfg))
    for name in (
        "RUNNER_SERVER_URL",
        "OMNIGENT_RUNNER_INITIAL_AUTH_TOKEN",
        "OMNIGENT_RUNNER_DELEGATED_AUTH",
        "OMNIGENT_RUNNEL_TUNNEL_BINDING_TOKEN",
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
        "OMNIGENT_RUNNER_SLICE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    # The runner-process singleton must not shadow per-test factories.
    monkeypatch.setattr(runner_entry, "_runner_auth_factory", None)
    yield state_dir


def _write_stored_oidc_token(state_dir: Path, server_url: str, token: str) -> None:
    """Simulate the user completing ``omnigent login`` for *server_url*.

    Writes a valid stored OIDC session token (1 h of remaining life) into the
    isolated ``auth_tokens.json`` — exactly what a completed login leaves on
    disk for credential discovery to find.

    :param state_dir: The isolated Omnigent state directory.
    :param server_url: Server URL the token is stored under.
    :param token: The session token string to store.
    :returns: None.
    """
    (state_dir / "auth_tokens.json").write_text(
        json.dumps({server_url.rstrip("/"): {"token": token, "expires_at": time.time() + 3600}})
    )


# ---------------------------------------------------------------------------
# Facet 1 — bootstrap invalidation must not permanently cache a missing
# credential fallback (delayed login is never rediscovered).
# ---------------------------------------------------------------------------


def test_delayed_relogin_is_rediscovered_after_bootstrap_invalidation(
    isolated_credentials: Path,
) -> None:
    """A login completed minutes after invalidation must be rediscovered.

    Journey: the host-bootstrapped runner's bearer expires and the server
    rejects it (the auth flow invalidates the bootstrap bearer); credential
    discovery finds nothing because the user's re-login is still in progress;
    the user completes ``omnigent login`` a few minutes later. The factory
    must then return the fresh credential — pre-fix it permanently cached the
    missing fallback and returns ``None`` forever, bricking the runner until
    process restart.

    :param isolated_credentials: Isolated Omnigent state directory fixture.
    :returns: None.
    """
    server_url = "http://127.0.0.1:59999"
    factory = _InitialAuthTokenFactory(_BOOTSTRAP_BEARER, server_url)
    assert factory() == _BOOTSTRAP_BEARER

    # The server rejected the bootstrap bearer (as _RunnerDatabricksAuth /
    # serve_tunnel do on 401 or an OAuth login redirect).
    assert factory.invalidate() is True

    # Login has not completed yet: no credential is correct at this instant.
    assert factory() is None

    # Minutes later the user's re-login completes and leaves a stored token.
    _write_stored_oidc_token(isolated_credentials, server_url, _FRESH_OIDC_TOKEN)

    # Rediscovery may sit behind a short bounded backoff (discovery can
    # shell out to the Databricks CLI, so hammering it on every callback
    # would be wrong) — but it must happen without a process restart. Poll
    # briefly; on the live bug the missing fallback is cached forever and
    # this still returns ``None`` at the deadline.
    deadline = time.monotonic() + 15
    token = factory()
    while token != _FRESH_OIDC_TOKEN and time.monotonic() < deadline:
        time.sleep(0.25)
        token = factory()
    assert token == _FRESH_OIDC_TOKEN, (
        "credential rediscovery after a delayed re-login returned "
        f"{token!r} instead of the fresh stored token (waited 15s): "
        "_InitialAuthTokenFactory permanently cached the missing fallback "
        "factory it resolved while login was still in progress, so the "
        "runner never recovers without a process restart"
    )


# ---------------------------------------------------------------------------
# Facet 2 — an established tunnel must survive a login redirect while renewal
# is unavailable, and reconnect once login completes.
# ---------------------------------------------------------------------------


class _DelayedLoginCredentials:
    """Well-behaved synthetic credential provider with a delayed re-login.

    Mirrors the ``_InitialAuthTokenFactory`` interface: returns the host
    bootstrap bearer until :meth:`invalidate`, then ``None`` while the user's
    login is in progress, then a fresh token once ``login_complete`` is set.
    Being well-behaved isolates the tunnel-loop facet from the facet-1
    caching bug.
    """

    def __init__(self) -> None:
        """Initialize with the bootstrap bearer still valid."""
        self._bootstrap: str | None = _BOOTSTRAP_BEARER
        self.login_complete = False

    def __call__(self) -> str | None:
        """Return the current credential, or ``None`` while login is pending.

        :returns: The bootstrap bearer, the fresh post-login token, or
            ``None`` while re-login is still in progress.
        """
        if self._bootstrap is not None:
            return self._bootstrap
        return _FRESH_OIDC_TOKEN if self.login_complete else None

    def invalidate(self) -> bool:
        """Discard the bootstrap bearer after a server rejection.

        :returns: ``True`` when the bootstrap bearer was discarded.
        """
        if self._bootstrap is None:
            return False
        self._bootstrap = None
        return True


async def test_established_tunnel_survives_login_redirect_and_reconnects() -> None:
    """An established tunnel must retry through a delayed re-login window.

    Journey: the runner's tunnel connects and serves (proving credentials);
    the server recycles the connection; the bearer has expired, so every
    reconnect upgrade bounces to the Databricks Apps OAuth login page while
    renewal is unavailable (the user's re-login takes minutes); the login
    then completes. The tunnel loop must keep retrying with backoff and
    reconnect on its own — pre-fix the immediate-refresh path raises a fatal
    ``RuntimeError`` on the first redirect, killing the tunnel task, so the
    runner never reconnects no matter when login completes.

    :returns: None.
    """
    creds = _DelayedLoginCredentials()
    upgrade_attempts = 0
    accepted = 0
    first_connected = asyncio.Event()
    reconnected_after_login = asyncio.Event()
    login_complete_server_side = False

    def process_request(connection: object, request: object) -> object | None:
        """Accept the first upgrade; bounce later ones to the OAuth login page.

        :param connection: The server-side WebSocket connection.
        :param request: The HTTP upgrade request.
        :returns: ``None`` to accept, or a 302 login redirect response.
        """
        nonlocal upgrade_attempts
        upgrade_attempts += 1
        if upgrade_attempts == 1:
            return None
        auth = request.headers.get("Authorization", "")  # type: ignore[attr-defined]
        if login_complete_server_side and auth == f"Bearer {_FRESH_OIDC_TOKEN}":
            return None
        response = connection.respond(http.HTTPStatus.FOUND, "")  # type: ignore[attr-defined]
        response.headers["Location"] = _OAUTH_LOGIN_URL
        return response

    async def handler(connection: object) -> None:
        """Serve one accepted tunnel connection, then recycle it.

        :param connection: The server-side WebSocket connection.
        :returns: None.
        """
        nonlocal accepted
        accepted += 1
        if accepted == 1:
            first_connected.set()
        else:
            reconnected_after_login.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(connection.recv(), timeout=5)  # type: ignore[attr-defined]
        with contextlib.suppress(Exception):
            await connection.close(1000)  # type: ignore[attr-defined]

    async def _noop_app(scope: object, receive: object, send: object) -> None:
        """Minimal ASGI app; the fake server never dispatches requests.

        :param scope: ASGI scope.
        :param receive: ASGI receive callable.
        :param send: ASGI send callable.
        :returns: None.
        """
        del scope, receive, send

    port = find_free_port()
    async with serve(handler, "127.0.0.1", port, process_request=process_request):
        tunnel_task = asyncio.create_task(
            serve_tunnel(
                _noop_app,
                server_url=f"http://127.0.0.1:{port}",
                runner_id="runner_delayed_relogin_e2e",
                runner_version="0.0.0-e2e",
                auth_token_factory=creds,
            )
        )
        try:
            await asyncio.wait_for(first_connected.wait(), timeout=15)

            # Let the tunnel take several redirect-rejected reconnect attempts
            # while renewal is unavailable (login still in progress). Pre-fix
            # the task dies fatally on the first redirect instead.
            deadline = time.monotonic() + 25
            while upgrade_attempts < 4 and not tunnel_task.done() and time.monotonic() < deadline:
                await asyncio.sleep(0.1)

            if tunnel_task.done():
                exc = tunnel_task.exception()
                pytest.fail(
                    "established tunnel died fatally instead of retrying "
                    f"through the delayed re-login window: {exc!r}. Expected "
                    "it to keep reconnecting with backoff and recover once "
                    "login completes."
                )

            # The user's re-login completes; the server accepts the fresh
            # bearer again. The tunnel must reconnect on its own.
            creds.login_complete = True
            login_complete_server_side = True

            reconnect_waiter = asyncio.create_task(reconnected_after_login.wait())
            done, _pending = await asyncio.wait(
                {tunnel_task, reconnect_waiter},
                timeout=30,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if tunnel_task in done:
                exc = tunnel_task.exception()
                pytest.fail(
                    f"tunnel task died after login completed instead of reconnecting: {exc!r}"
                )
            reconnect_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reconnect_waiter
            assert reconnected_after_login.is_set(), (
                "tunnel did not reconnect within 30s of the delayed re-login "
                "completing; established tunnels must survive a failed or "
                "delayed login and reconnect when credentials return"
            )
        finally:
            tunnel_task.cancel()
            await asyncio.gather(tunnel_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Facet 3 — the synchronous credential provider must not execute on the
# running event loop during async HTTP authentication.
# ---------------------------------------------------------------------------


class _Ok200Handler(BaseHTTPRequestHandler):
    """Answer 200 OK to every GET, standing in for an Omnigent endpoint."""

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default request logging.

        :param format: printf-style format string.
        :param args: Format arguments.
        :returns: None.
        """
        del format, args

    def do_GET(self) -> None:
        """Respond 200 to any GET.

        :returns: None.
        """
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def ok_server() -> Iterator[str]:
    """Spin up a local HTTP server that answers 200 to every GET.

    :yields: The base URL of the server.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Ok200Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_sync_credential_provider_runs_off_the_event_loop(ok_server: str) -> None:
    """The credential provider must run in a worker thread, not on the loop.

    ``_RunnerDatabricksAuth``'s provider can shell out to the Databricks CLI
    (~0.5 s) or block on an HTTP mint. Executed on the running event loop it
    stalls every other coroutine (heartbeats, stream relays) and breaks
    providers that require a worker thread. The provider here records where
    it executed; pre-fix it observes a running event loop.

    :param ok_server: Base URL of the local 200-OK server fixture.
    :returns: None.
    """
    execution_contexts: list[str] = []

    def provider() -> str:
        """Record whether this call executes on the event loop.

        :returns: A synthetic bearer token.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            execution_contexts.append("worker-thread")
        else:
            execution_contexts.append("event-loop")
        return "synthetic-bearer"

    async def drive() -> None:
        """Make one authenticated request through the real async client.

        :returns: None.
        """
        auth = _RunnerDatabricksAuth(provider, server_url=ok_server)
        async with httpx.AsyncClient(auth=auth) as client:
            response = await client.get(f"{ok_server}/v1/sessions/s/agent/contents")
            assert response.status_code == 200

    asyncio.run(drive())

    assert execution_contexts, "credential provider was never invoked"
    assert all(ctx == "worker-thread" for ctx in execution_contexts), (
        "synchronous credential provider executed on the running asyncio "
        f"event loop (observed contexts: {execution_contexts}); it must run "
        "in a worker thread so blocking lookup/refresh cannot stall the "
        "runner's event loop or break providers that require a thread"
    )


# ---------------------------------------------------------------------------
# Facet 4 — after the bootstrap fallback resolves, a rejected credential must
# reach the resolved provider (invalidate + re-mint), not be re-sent forever.
# ---------------------------------------------------------------------------


class _MintAndResourceHandler(BaseHTTPRequestHandler):
    """Local server: managed-mint endpoint plus a bearer-checked resource.

    ``POST …/token`` mints ``minted-<n>`` (incrementing). ``GET`` of any
    other path rejects the expired bootstrap bearer and the first minted
    token with 401 (both revoked server-side), and accepts any later mint —
    so recovery only needs the client to actually re-mint.
    """

    mint_count = 0
    lock = threading.Lock()

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default request logging.

        :param format: printf-style format string.
        :param args: Format arguments.
        :returns: None.
        """
        del format, args

    def do_POST(self) -> None:
        """Mint the next owner token.

        :returns: None.
        """
        cls = type(self)
        with cls.lock:
            cls.mint_count += 1
            token = f"minted-{cls.mint_count}"
        body = json.dumps({"token": token, "expires_at": time.time() + 3600}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Serve the resource, rejecting revoked bearers with 401.

        :returns: None.
        """
        bearer = self.headers.get("Authorization", "")
        revoked = {f"Bearer {_BOOTSTRAP_BEARER}", "Bearer minted-1"}
        if bearer in revoked or not bearer.startswith("Bearer minted-"):
            body = b"unauthorized"
            self.send_response(401)
        else:
            body = b"{}"
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def mint_server() -> Iterator[str]:
    """Spin up the mint + resource server.

    :yields: The base URL of the server.
    """
    _MintAndResourceHandler.mint_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MintAndResourceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_rejection_after_bootstrap_fallback_reaches_the_resolved_provider(
    isolated_credentials: Path,
    monkeypatch: pytest.MonkeyPatch,
    mint_server: str,
) -> None:
    """A rejected minted credential must be invalidated and re-minted.

    Journey: the host-bootstrapped runner's bearer expires; the first
    callback 401s, the bootstrap bearer is invalidated, and the fallback
    resolves to the managed-mint provider (``minted-1``). The server later
    revokes that credential too (e.g. a signing-key rotation) and 401s it.
    The next callback must reach the resolved provider — invalidate its
    cache and re-mint — and succeed. Pre-fix
    ``_InitialAuthTokenFactory.invalidate()`` returns ``False`` without
    forwarding to the provider, so every retry re-sends the same revoked
    token and the runner's callbacks 401 forever.

    :param isolated_credentials: Isolated Omnigent state directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :param mint_server: Base URL of the mint + resource server fixture.
    :returns: None.
    """
    del isolated_credentials
    monkeypatch.setenv(RUNNER_DELEGATED_AUTH_ENV_VAR, "1")
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "delayed-relogin-e2e-binding-token")

    factory = _InitialAuthTokenFactory(_BOOTSTRAP_BEARER, mint_server)
    auth = _RunnerDatabricksAuth(factory, server_url=mint_server)
    resource_url = f"{mint_server}/v1/sessions/s/agent/contents"

    with httpx.Client(auth=auth) as client:
        # Callback 1: bootstrap bearer 401s -> invalidated -> fallback
        # resolves and mints minted-1 -> the retry discovers minted-1 is
        # ALSO revoked (rotation happened while the runner was cut off).
        first = client.get(resource_url)
        assert first.status_code == 401

        # Callback 2: the rejection must reach the resolved mint provider so
        # it re-mints a fresh credential the server accepts.
        second = client.get(resource_url)

    assert second.status_code == 200, (
        f"runner callback still fails (HTTP {second.status_code}) after its "
        "minted credential was revoked, even though re-minting would "
        "succeed: invalidate() on the bootstrap wrapper never reaches the "
        "resolved managed-mint provider, so every retry re-sends the same "
        "rejected token and the runner is bricked until restart "
        f"(mints performed: {_MintAndResourceHandler.mint_count})"
    )
