"""Runner subprocess entry point.

Launched by the CLI when spawning the runner as a separate process.
Reads process wiring from environment variables set by the parent:
- ``RUNNER_SERVER_URL``: Omnigent server base URL for outbound calls
  (spec fetch, response resolution, and WS tunnel registration).
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Generator
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
from fastapi import FastAPI

from omnigent._platform import IS_WINDOWS
from omnigent.inner import _proc
from omnigent.runner.transports.ws_tunnel.serve import RUNNER_TUNNEL_REJECTION_PREFIX
from omnigent.version import VERSION

if TYPE_CHECKING:
    from types import TracebackType

    from omnigent.runner.native import ResolvedSpec
    from omnigent.runner.transports.ws_tunnel.serve import _ASGIApp

_RUNNER_SERVER_URL_ENV_VAR = "RUNNER_SERVER_URL"
_RUNNER_PREWARM_SPEC_PATH_ENV_VAR = "RUNNER_PREWARM_SPEC_PATH"
# The runner advertises the omnigent version it is actually running (shared
# with the CLI/server/host) instead of a hard-coded placeholder.
_RUNNER_VERSION = VERSION
_RUNNER_CONFIG_HOME_ENV_VAR = "OMNIGENT_CONFIG_HOME"
_DEFAULT_RUNNER_IDLE_TIMEOUT_S = 60 * 60
_RUNNER_IDLE_MONITOR_MAX_POLL_INTERVAL_S = 60.0
# The runner offloads short native-CLI/IPC ops via asyncio.to_thread. Python's
# default executor sizes to min(32, cpu+4) threads, which on a many-core host
# inflates RSS (thread stacks + glibc arenas) for little benefit. Cap it small.
_DEFAULT_RUNNER_THREADPOOL_MAX_WORKERS = 8
_RUNNER_THREADPOOL_MAX_WORKERS_ENV_VAR = "OMNIGENT_RUNNER_THREADPOOL_MAX_WORKERS"
# Backstop on how long a graceful (idle-reaper) shutdown waits for the tunnel
# to drain its session streams and complete its close handshake before we give
# up and tear it down anyway. Comfortably above serve_tunnel's own drain +
# close-handshake budgets (~10s combined) so a healthy remote round-trip
# finishes cleanly; a tunnel wedged past this is cancelled so shutdown can't
# hang forever.
_GRACEFUL_SHUTDOWN_TUNNEL_TIMEOUT_S = 15.0
# Re-mint a delegated runner's owner JWT this many seconds before it
# expires, so a live session's HTTP callbacks never present an expired
# token. Well under the server-side token TTL.
_MANAGED_MINT_REFRESH_SKEW_S = 300.0
_logger = logging.getLogger(__name__)

# Module-level singleton set once at runner startup. All later
# _make_auth_token_factory() calls return this instance so every call site
# shares the proxy bearer, even after RUNNER_INITIAL_AUTH_TOKEN has been
# popped from the environment.
_runner_auth_factory: Callable[[], str | None] | None = None


def _set_runner_auth_factory(factory: Callable[[], str | None] | None) -> None:
    """Set the runner-process auth factory singleton.

    Called once at startup so every subsequent :func:`_make_auth_token_factory`
    call returns this instance instead of building a new one. Exposed as a
    function rather than a bare module assignment so callers (including those
    running as ``__main__``, where the module object differs from the imported
    ``omnigent.runner._entry``) can set it on the canonical module.

    :param factory: The auth token factory to install as the singleton.
    """
    global _runner_auth_factory
    _runner_auth_factory = factory


def _server_url_from_env() -> str:
    """Return the required Omnigent server URL from the runner environment.

    :returns: Server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :raises RuntimeError: If ``RUNNER_SERVER_URL`` is missing or
        empty.
    """
    server_url = os.environ.get(_RUNNER_SERVER_URL_ENV_VAR)
    if server_url is None or not server_url.strip():
        raise RuntimeError(
            f"{_RUNNER_SERVER_URL_ENV_VAR} is required for the runner WebSocket tunnel"
        )
    return server_url.strip()


def _runner_config_path() -> Path:
    """Return the global Omnigent config path visible to the runner.

    Respects :envvar:`OMNIGENT_CONFIG_HOME` for test isolation and
    subprocess consistency with the CLI/onboarding layer.

    :returns: Config path, e.g. ``Path("~/.omnigent/config.yaml")``.
    """
    config_home = os.environ.get(_RUNNER_CONFIG_HOME_ENV_VAR)
    if config_home:
        return Path(config_home).expanduser() / "config.yaml"
    return Path.home() / ".omnigent" / "config.yaml"


def _load_runner_idle_timeout_s_from_config() -> float:
    """Load the runner inactivity timeout from config.

    Reads ``runner.idle_timeout_s`` from the global config file. Missing
    config or missing key defaults to 1 hour. A value of ``0`` disables the
    inactivity watchdog. Negative, boolean, or non-numeric values fail loud
    during runner startup so the user does not get silently different
    lifecycle behavior than requested.

    :returns: Idle timeout in seconds, e.g. ``3600.0``. ``0.0`` disables
        the watchdog.
    :raises RuntimeError: If ``runner.idle_timeout_s`` is invalid.
    """
    import yaml

    path = _runner_config_path()
    if not path.exists():
        return float(_DEFAULT_RUNNER_IDLE_TIMEOUT_S)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"failed to read runner config from {path}: {exc}") from exc
    if not isinstance(raw, dict):
        return float(_DEFAULT_RUNNER_IDLE_TIMEOUT_S)
    runner_cfg = raw.get("runner")
    if runner_cfg is None:
        return float(_DEFAULT_RUNNER_IDLE_TIMEOUT_S)
    if not isinstance(runner_cfg, dict):
        raise RuntimeError("runner config must be a mapping")
    raw_timeout = runner_cfg.get("idle_timeout_s")
    if raw_timeout is None:
        return float(_DEFAULT_RUNNER_IDLE_TIMEOUT_S)
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        raise RuntimeError("runner.idle_timeout_s must be a non-negative number of seconds")
    timeout_s = float(raw_timeout)
    if timeout_s < 0:
        raise RuntimeError("runner.idle_timeout_s must be a non-negative number of seconds")
    return timeout_s


def _runner_threadpool_max_workers() -> int:
    """Load the runner's asyncio default-executor size.

    Reads ``runner.threadpool_max_workers`` from the global config file,
    overridable by ``OMNIGENT_RUNNER_THREADPOOL_MAX_WORKERS``. Missing config
    defaults to 8. Non-positive, boolean, or non-integer values fail loud so a
    misconfiguration surfaces at startup rather than silently reverting.

    :returns: Positive worker count for the runner threadpool.
    :raises RuntimeError: If the configured value is invalid.
    """
    raw_env = os.environ.get(_RUNNER_THREADPOOL_MAX_WORKERS_ENV_VAR)
    if raw_env is not None and raw_env.strip():
        try:
            workers = int(raw_env)
        except ValueError as exc:
            raise RuntimeError(
                f"{_RUNNER_THREADPOOL_MAX_WORKERS_ENV_VAR} must be a positive integer"
            ) from exc
        if workers < 1:
            raise RuntimeError(
                f"{_RUNNER_THREADPOOL_MAX_WORKERS_ENV_VAR} must be a positive integer"
            )
        return workers

    import yaml

    path = _runner_config_path()
    if not path.exists():
        return _DEFAULT_RUNNER_THREADPOOL_MAX_WORKERS
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"failed to read runner config from {path}: {exc}") from exc
    if not isinstance(raw, dict):
        return _DEFAULT_RUNNER_THREADPOOL_MAX_WORKERS
    runner_cfg = raw.get("runner")
    if runner_cfg is None:
        return _DEFAULT_RUNNER_THREADPOOL_MAX_WORKERS
    if not isinstance(runner_cfg, dict):
        raise RuntimeError("runner config must be a mapping")
    raw_workers = runner_cfg.get("threadpool_max_workers")
    if raw_workers is None:
        return _DEFAULT_RUNNER_THREADPOOL_MAX_WORKERS
    if isinstance(raw_workers, bool) or not isinstance(raw_workers, int) or raw_workers < 1:
        raise RuntimeError("runner.threadpool_max_workers must be a positive integer")
    return raw_workers


async def _run_inactivity_monitor(
    *,
    idle_timeout_s: float,
    get_last_activity: Callable[[], float],
    has_active_work: Callable[[], bool],
    request_shutdown: Callable[[], None],
    poll_interval_s: float | None = None,
) -> None:
    """Request runner shutdown after the configured idle window expires.

    The monitor only shuts down when there has been no real runner work for
    ``idle_timeout_s`` and no agent work is active. If the timeout expires
    while an agent turn is running, the monitor keeps waiting and exits soon
    after the active work clears unless new activity resets the timer.

    :param idle_timeout_s: Idle window in seconds, e.g. ``3600.0``. ``0``
        disables the monitor.
    :param get_last_activity: Callback returning the most recent real
        activity time from the event loop's monotonic clock.
    :param has_active_work: Callback returning whether delivery-critical
        work is outstanding (turns, live async tools, timers, approvals).
    :param request_shutdown: Callback that requests graceful runner shutdown.
    :param poll_interval_s: Optional test override for the monitor cadence,
        e.g. ``0.01``. ``None`` derives a bounded production cadence from
        ``idle_timeout_s``.
    :returns: None.
    """
    if idle_timeout_s <= 0:
        return
    loop = asyncio.get_running_loop()
    if poll_interval_s is None:
        poll_interval_s = min(
            _RUNNER_IDLE_MONITOR_MAX_POLL_INTERVAL_S,
            max(1.0, idle_timeout_s / 30.0),
        )
    while True:
        elapsed_s = loop.time() - get_last_activity()
        if elapsed_s >= idle_timeout_s:
            if has_active_work():
                await asyncio.sleep(poll_interval_s)
                continue
            _logger.info(
                "runner idle timeout reached after %.1fs with no active work; shutting down",
                elapsed_s,
            )
            request_shutdown()
            return
        await asyncio.sleep(min(poll_interval_s, idle_timeout_s - elapsed_s))


class _RunnerDatabricksAuth(httpx.Auth):
    """httpx Auth that mints a fresh Databricks OAuth token per request.

    Used by the runner's HTTP client for callbacks to the Omnigent server
    (agent-bundle downloads, response lookups, file APIs, idle
    notifications). Tokens are refreshed transparently so
    long-running sessions survive the 1-hour OAuth token lifetime.

    When no Databricks credentials are available (e.g. local
    unauthenticated servers), the auth flow is a no-op.
    """

    def __init__(
        self,
        factory: Callable[[], str | None] | None,
        server_url: str | None = None,
    ) -> None:
        """
        :param factory: Sync callable that returns a fresh bearer
            token, e.g. the return value of
            :func:`_make_auth_token_factory`. ``None`` disables
            auth (local unauthenticated servers).
        :param server_url: Omnigent server URL used to look up the ``?o=``
            workspace selector for the ``X-Databricks-Org-Id`` routing
            header. Defaults to ``RUNNER_SERVER_URL`` so existing callers
            (which pass only the factory) need no change.
        """
        self._factory = factory
        self._server_url = server_url or os.environ.get(_RUNNER_SERVER_URL_ENV_VAR)

    def auth_flow(
        self,
        request: httpx.Request,
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Inject a fresh ``Authorization`` header before each request.

        Fails closed: when the factory is configured but returns no
        token (transient SDK failure), raises rather than silently
        sending an unauthenticated request. Retries once with a freshly
        minted token on either:

        - HTTP 401 (the standard "your bearer is invalid" response), or
        - a 3xx redirect whose ``Location`` points at the Databricks
          Apps OAuth login flow (``/oidc/`` or ``/.auth/``). The Apps
          front door does NOT return 401 for an expired bearer; it
          bounces the request to ``/oidc/oauth2/v2.0/authorize`` with
          a 302. Without this branch, every subsequent runner→AP
          callback after token expiry surfaces as a redirect that
          the caller treats as a hard error (e.g. MCP-proxy tool
          calls hang and "never resolve").

        :param request: The outgoing httpx request.
        :yields: The request with the auth header set, or
            unmodified when no factory is configured.
        :raises httpx.RequestError: When the factory is configured
            but returns no token.
        """
        # Workspace routing: name the workspace or the request routes to the
        # account (the forwarder's POST /events otherwise 403s). Empty when
        # none recorded. Set once here; it persists across the retry yield.
        if self._server_url:
            from omnigent.cli_auth import databricks_request_headers

            request.headers.update(databricks_request_headers(self._server_url))
        if self._factory is not None:
            token = self._factory()
            if not token:
                if getattr(self._factory, "declined", False):
                    # The server definitively refuses to mint for this runner
                    # (managed mint factory hit HTTP 400/404 after install —
                    # e.g. its construction probe lost a boot race to a
                    # no-auth server). Bare requests are correct there; do
                    # NOT fail closed or the runner bricks every callback.
                    yield request
                    return
                raise httpx.RequestError("Databricks token refresh returned no token")
            request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if self._factory is None:
            return
        if _is_login_redirect_or_unauthorized(response):
            _invalidate_auth_token_factory(self._factory)
            token = self._factory()
            if token:
                request.headers["Authorization"] = f"Bearer {token}"
                yield request


def _is_login_redirect_or_unauthorized(response: httpx.Response) -> bool:
    """Return ``True`` when ``response`` is a re-auth signal.

    Treats both HTTP 401 and a 3xx redirect to the Databricks Apps
    OAuth login flow as a "the bearer is no good, mint a new one"
    signal. The Apps proxy returns 302→``/oidc/oauth2/v2.0/authorize``
    (with ``redirect_uri`` ending at ``/.auth/callback``) for expired
    bearers instead of the standard 401, so callers that only check
    for 401 silently fail.

    Returns ``False`` for unrelated 3xx (e.g. an application-level
    redirect to another resource) so the caller doesn't accidentally
    re-mint on every redirect.

    :param response: The httpx response to classify.
    :returns: ``True`` when the response indicates the request should
        be retried with a fresh token, ``False`` otherwise.
    """
    if response.status_code in (401, 403):
        # Databricks Apps returns 403 "Invalid Token" for an expired bearer
        # in addition to the 302→/oidc/ bounce; treat both as re-auth signals.
        return True
    if not response.is_redirect:
        return False
    location = response.headers.get("location", "")
    # Match the Apps front-door OAuth flow specifically. ``/oidc/`` is
    # the OAuth provider mount; ``/.auth/`` covers the callback path
    # (e.g. ``/.auth/callback``) the Apps proxy uses when stitching the
    # browser-style flow back together.
    return "/oidc/" in location or "/.auth/" in location


def _invalidate_auth_token_factory(factory: Callable[[], str | None]) -> bool:
    """Invalidate a bootstrap token factory when it supports that operation.

    Ordinary token factories already return a fresh token on each call and
    expose no invalidation hook. A host-bootstrap factory holds its initial
    bearer until the server rejects it; invalidating switches the factory to
    the runner's existing refreshable credential path.

    :param factory: Runner auth token factory.
    :returns: ``True`` when a bootstrap token was invalidated.
    """
    invalidate = getattr(factory, "invalidate", None)
    if not callable(invalidate):
        return False
    return bool(invalidate())


class _InitialAuthTokenFactory:
    """Use a host bearer until rejection, then lazily resolve runner auth."""

    def __init__(self, token: str, server_url: str) -> None:
        """
        :param token: Current bearer obtained from the connected host.
        :param server_url: Omnigent server URL used by the fallback resolver.
        """
        self._initial_token: str | None = token
        self._last_initial_token: str = token  # retained for managed-mint proxy auth
        self._server_url = server_url
        self._fallback_factory: Callable[[], str | None] | None = None
        self._fallback_resolved = False
        self._no_credential_logged = False
        self._lock = threading.Lock()

    def __call__(self) -> str | None:
        """Return the host bearer or a token from the lazy local fallback."""
        with self._lock:
            if self._initial_token is not None:
                return self._initial_token
            if not self._fallback_resolved:
                self._fallback_factory = _make_auth_token_factory(
                    self._server_url,
                    _allow_initial_token=False,
                    _proxy_bearer=self._last_initial_token,
                )
                self._fallback_resolved = True
            token = self._fallback_factory() if self._fallback_factory is not None else None
            # Managed mint cannot renew itself when its proxy bearer expires —
            # the injected host bearer before a first mint, or the minted JWT
            # once a session outlives it. Re-resolve SDK/OIDC in the same call
            # so the request that hit the failure still gets a credential.
            if token is None and getattr(self._fallback_factory, "proxy_auth_failed", False):
                self._fallback_factory = _make_auth_token_factory(
                    self._server_url,
                    _allow_initial_token=False,
                    _allow_delegated_mint=False,
                )
                token = self._fallback_factory() if self._fallback_factory is not None else None
            if self._fallback_factory is None and not self._no_credential_logged:
                # This state is terminal for the process, so say it once
                # rather than on every subsequent callback.
                self._no_credential_logged = True
                _logger.error(
                    "host bootstrap bearer expired and no SDK/OIDC credential is available "
                    "to renew it; run `databricks auth login` to re-authenticate"
                )
            return token

    @property
    def declined(self) -> bool:
        """True when the inner fallback factory has definitively declined."""
        with self._lock:
            f = self._fallback_factory
            return getattr(f, "declined", False) and not getattr(f, "proxy_auth_failed", False)

    def invalidate(self) -> bool:
        """Discard the host bearer so the next call resolves local auth."""
        with self._lock:
            if self._initial_token is None:
                return False
            self._initial_token = None
            _logger.info("host bootstrap bearer rejected; resolving runner-local auth")
            return True


def _make_auth_token_factory(
    server_url: str | None = None,
    *,
    _allow_initial_token: bool = True,
    _allow_delegated_mint: bool = True,
    _proxy_bearer: str | None = None,
) -> Callable[[], str | None] | None:
    """Build a callable that mints fresh auth tokens.

    Resolution order:
      1. Host's current bearer, when injected for runner bootstrap. This is
         used until rejection; local refreshable auth resolves lazily.
      2. Host-delegated runner token, when the host launch marker and
         binding token are present.
      3. Stored OIDC token from ``~/.omnigent/auth_tokens.json``
         (populated by ``omnigent login``), keyed by ``server_url``.
      4. Databricks OAuth token (refreshed via the SDK) — host-keyed
         when a Databricks Apps pointer record is stored for
         ``server_url`` (``omnigent login <apps-url>``), ambient
         otherwise.

    Returns ``None`` when no credentials are available.

    :param server_url: Server URL to look up the stored OIDC token
        for. When omitted, falls back to the ``RUNNER_SERVER_URL``
        env var — the runner subprocess always has this set, but
        non-runner callers (e.g. ``omnigent host``) must pass
        it explicitly or the OIDC token won't be discovered and the
        factory will silently fall through to the Databricks path.

    Used by:
    - :func:`serve_tunnel` for the WebSocket ``Authorization`` header
      (refreshed on each reconnect).
    - :class:`_RunnerDatabricksAuth` for the httpx client
      (refreshed on each HTTP callback to the Omnigent server).
    - ``omnigent/host/connect.py`` for the host tunnel's WS upgrade
      headers.

    :returns: A sync callable returning a bearer token string, or
        ``None`` when no refresh mechanism is available.
    """
    # Return the runner-process singleton when it has been set and no
    # caller-specific overrides are requested. This ensures every harness
    # setup or terminal-creation call made after runner startup reuses the
    # factory that already has the proxy bearer, rather than constructing a
    # fresh one after RUNNER_INITIAL_AUTH_TOKEN has been popped from env.
    # Also match when the caller explicitly passes the same URL the runner
    # was started with — in-runner helpers like native_policy_hook pass
    # server_url explicitly but still want the shared factory.
    _runner_url = os.environ.get(_RUNNER_SERVER_URL_ENV_VAR)
    if (
        _runner_auth_factory is not None
        and _allow_initial_token
        and _allow_delegated_mint
        and _proxy_bearer is None
        and (server_url is None or server_url == _runner_url)
    ):
        return _runner_auth_factory
    resolved_server_url = server_url or os.environ.get(_RUNNER_SERVER_URL_ENV_VAR)

    # Consume the host bearer before any credential discovery. Removing it
    # from os.environ here ensures later harness/terminal children cannot
    # inherit it even if a spawn path bypasses the standard secret scrubber.
    from omnigent.runner.identity import (
        RUNNER_DELEGATED_AUTH_ENV_VAR,
        RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR,
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    )

    initial_token = (
        os.environ.pop(RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR, "").strip()
        if _allow_initial_token
        else ""
    )
    if initial_token and resolved_server_url:
        _logger.info("using host-provided bearer for runner bootstrap")
        return _InitialAuthTokenFactory(initial_token, resolved_server_url)

    from omnigent.inner.databricks_executor import (
        DatabricksAuthError,
        _DatabricksBearerAuth,
        _resolve_databricks_auth,
    )

    # Prefer the host-launched runner's owner-bound capability so user
    # credentials stay out of the runner and credential discovery is skipped.
    delegated_auth = os.environ.get(RUNNER_DELEGATED_AUTH_ENV_VAR, "").strip() == "1"
    binding_token = os.environ.get(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "").strip()
    if _allow_delegated_mint and delegated_auth and resolved_server_url and binding_token:
        delegated_factory = _make_managed_mint_factory(
            resolved_server_url, binding_token, proxy_bearer=_proxy_bearer
        )
        if delegated_factory is not None:
            return delegated_factory

    # Reused Databricks SDK auth, resolved once on first use and cached
    # here for the life of the factory. Reusing one Config is the whole
    # point: the SDK serves the minted OAuth token from its in-memory
    # cache and only re-runs the Databricks CLI (~0.5s) when the token
    # nears expiry. The previous implementation built a fresh Config on
    # every call (via _read_databrickscfg), shelling out to the CLI on
    # EVERY runner->AP request — ~6.5s across the ~13 requests of session
    # establish alone, plus the same tax on every later turn.
    # ``sdk_auth_resolved`` is the "have we tried resolving yet" flag;
    # ``sdk_auth`` is the (possibly ``None``) reused auth once resolved.
    sdk_auth: _DatabricksBearerAuth | None = None
    sdk_auth_resolved = False

    def _sdk_token() -> str | None:
        """
        Return a bearer token from the reused SDK auth, or ``None``.

        Resolves the SDK auth on first call and reuses it thereafter, so
        repeat fetches hit the SDK's in-memory token cache instead of
        rebuilding ``Config`` / re-shelling to the Databricks CLI.

        :returns: Bearer token string, or ``None`` when no Databricks
            credentials resolve.
        """
        nonlocal sdk_auth, sdk_auth_resolved
        if not sdk_auth_resolved:
            # A stored Databricks Apps pointer record (from
            # ``omnigent login <apps-url>``) names the exact workspace
            # the Apps edge accepts tokens from, so it beats ambient
            # profile resolution.
            from omnigent.cli_auth import load_databricks_workspace_host

            workspace_host = (
                load_databricks_workspace_host(resolved_server_url)
                if resolved_server_url
                else None
            )
            try:
                if workspace_host is not None:
                    sdk_auth, _host = _resolve_databricks_auth(host=workspace_host)
                else:
                    sdk_auth, _host = _resolve_databricks_auth()
            except (DatabricksAuthError, ImportError, ValueError):
                sdk_auth = None
            sdk_auth_resolved = True
        if sdk_auth is None:
            return None
        try:
            return sdk_auth.current_token()
        except DatabricksAuthError:
            return None

    def _factory() -> str | None:
        """Return a fresh auth token.

        Checks the stored OIDC token first (from ``omnigent login``),
        then falls back to the reused Databricks SDK auth.

        :returns: Bearer token string, or ``None`` if no credentials
            are configured.
        """
        # Check stored OIDC token first.
        if resolved_server_url:
            from omnigent.cli_auth import load_token

            oidc_token = load_token(resolved_server_url)
            if oidc_token:
                return oidc_token
        return _sdk_token()

    # Probe once to check if a user credential is available.
    try:
        if _factory() is not None:
            return _factory
    except (ValueError, OSError, ImportError):
        pass

    # Managed-sandbox fallback: no user credential resolved (no stored
    # OIDC token, no Databricks config), but a managed runner still holds
    # its tunnel binding token. Authenticate its HTTP callbacks (and the
    # tunnel bearer) with a short-lived owner JWT the server mints against
    # that binding token — refreshed on demand, so there is no static
    # credential at rest and no fixed session-length cap.
    if _allow_delegated_mint and resolved_server_url:
        try:
            fallback_binding_token = _runner_tunnel_binding_token_from_env()
        except RuntimeError:
            fallback_binding_token = None
        if fallback_binding_token is not None:
            return _make_managed_mint_factory(resolved_server_url, fallback_binding_token)
    return None


def _make_managed_mint_factory(
    server_url: str,
    binding_token: str,
    *,
    proxy_bearer: str | None = None,
) -> Callable[[], str | None] | None:
    """Build a token factory that mints a managed runner's owner JWT.

    For a server-managed sandbox runner with no user credential of its
    own: mint a short-lived owner JWT from ``POST /v1/runners/{id}/token``,
    authenticated by the runner's tunnel binding token, and cache it in
    memory. The cached token is reused until it nears expiry, then
    re-minted — so a managed session runs arbitrarily long without its
    auth expiring (no fixed session-length cap), and no long-lived
    credential is ever written to the sandbox environment.

    The same factory feeds both the WS tunnel bearer and the httpx
    callback client (see :func:`_make_auth_token_factory` callers), so one
    credential authenticates every runner->server surface.

    :param server_url: Omnigent server base URL, e.g.
        ``"https://omnigent.example.com"``.
    :param binding_token: The runner's tunnel binding token (the sandbox's
        only credential), presented to the mint endpoint.
    :param proxy_bearer: Optional bearer to include on the mint request so
        an Apps proxy lets it through. Used when a host-injected initial
        bearer is available (the minted JWT takes over on subsequent
        refreshes).
    :returns: A sync callable returning a fresh owner JWT, or ``None`` only
        when the server *definitively* will not mint for this runner (HTTP
        400 no-auth/header mode, 404 older server without the endpoint, or a
        Databricks Apps OAuth redirect before the request reaches the app) —
        the runner then uses the legacy credential path. A *transient* probe
        failure still installs the factory, which re-mints on the next
        callback (so a blip at boot does not leave the runner unauthenticated
        until process restart). If such a post-install mint then gets a
        definitive refusal, the factory latches ``declined`` and returns
        ``None`` thereafter, and :class:`_RunnerDatabricksAuth` falls back to
        bare requests. A post-install 401/403 with no still-valid cache
        latches ``proxy_auth_failed`` instead, which
        :class:`_InitialAuthTokenFactory` answers by re-resolving SDK/OIDC.
    """
    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)
    mint_url = f"{server_url.rstrip('/')}/v1/runners/{runner_id}/token"

    # Construction probe. Decline to install the factory ONLY when the
    # server definitively will not mint for this runner — HTTP 400 (no auth
    # provider / header mode), 404 (an older server without the endpoint), or
    # an Apps OAuth redirect that happens before the request reaches Omnigent.
    # Every other outcome installs the factory: a success seeds the cache; a
    # transient failure (network blip, 5xx, timeout) installs it anyway so the
    # next callback re-mints, rather than leaving the runner unauthenticated
    # until process restart.
    factory = _ManagedMintTokenFactory(
        mint_url, server_url, binding_token, proxy_bearer=proxy_bearer
    )
    factory()
    if factory.declined or factory.proxy_auth_failed:
        return None
    return factory


class _ManagedMintTokenFactory:
    """Callable that mints (and caches) a managed runner's owner JWT.

    Each call returns the cached JWT until it nears expiry, then re-mints
    via :func:`_mint_managed_owner_token`. When a mint gets a *definitive*
    refusal (HTTP 400 no-auth/header mode, 404 older server, or an Apps OAuth
    redirect), the
    :attr:`declined` latch is set and every subsequent call returns
    ``None`` without touching the network —
    :meth:`_RunnerDatabricksAuth.auth_flow` reads the latch to send bare
    requests instead of failing closed. The latch matters when the
    construction probe loses a boot race (a connection error installs the
    factory, then the first real mint learns the server never mints).

    A mint 401/403 with no still-valid cached token latches
    :attr:`proxy_auth_failed` instead: the credential presented to the proxy
    died (the seeded host bearer, or the minted JWT once a session outlives
    it), so callers should re-resolve SDK/OIDC rather than send bare requests.
    """

    def __init__(
        self,
        mint_url: str,
        server_url: str,
        binding_token: str,
        *,
        proxy_bearer: str | None = None,
    ) -> None:
        """
        :param mint_url: Fully-qualified ``/v1/runners/{id}/token`` URL.
        :param server_url: Omnigent server base URL.
        :param binding_token: The runner's tunnel binding token.
        :param proxy_bearer: Optional bearer for the Apps proxy. Seeded with
            the host's initial bearer; replaced by the minted JWT after the
            first successful mint so the proxy sees a valid credential on
            every subsequent refresh.
        """
        self._mint_url = mint_url
        self._server_url = server_url
        self._binding_token = binding_token
        self._proxy_bearer = proxy_bearer
        self._cached_token: str | None = None
        self._cached_expires_at = 0.0
        self.declined = False
        # Set when mint fails with 401/403 on the proxy bearer (not a server
        # refusal). Unlike ``declined``, this means the caller should try
        # another credential path (SDK/OIDC) rather than sending bare requests.
        self.proxy_auth_failed = False

    def __call__(self) -> str | None:
        """Return a fresh owner JWT, or ``None``.

        :returns: The cached or freshly-minted JWT; ``None`` after a
            definitive server decline (sets :attr:`declined`), after a mint
            401/403 with no still-valid cached token (sets
            :attr:`proxy_auth_failed`), or on a transient mint failure with
            no still-valid cached token.
        """
        if self.declined:
            return None
        now = time.time()
        if (
            self._cached_token is not None
            and now < self._cached_expires_at - _MANAGED_MINT_REFRESH_SKEW_S
        ):
            return self._cached_token
        try:
            token, expires_at = _mint_managed_owner_token(
                self._mint_url,
                self._server_url,
                self._binding_token,
                proxy_bearer=self._proxy_bearer,
            )
        except httpx.HTTPStatusError as exc:
            response = exc.response
            if response.status_code in (400, 404) or (
                response.is_redirect and _is_login_redirect_or_unauthorized(response)
            ):
                # Only treat as a definitive refusal if we have never
                # successfully minted. A 400/404 mid-session (e.g. during an
                # IP ACL flip) is transient — the server already proved it
                # mints for this runner.
                if self._cached_token is None:
                    self.declined = True
                    return None
                return self._still_valid_cached_token(now)
            if response.status_code in (401, 403):
                # The proxy bearer is expired or invalid — the seeded host
                # bearer, or the minted JWT once a session outlives it. With
                # no still-valid cache left the mint loop cannot renew itself:
                # latch proxy_auth_failed so the caller re-resolves SDK/OIDC
                # instead of failing closed on every callback.
                cached = self._still_valid_cached_token(now)
                if cached is None:
                    self.proxy_auth_failed = True
                return cached
            return self._still_valid_cached_token(now)
        except (httpx.HTTPError, ValueError, KeyError, OSError):
            # Transient mint failure: keep serving the cached token while
            # it is still valid; otherwise report "no token" and let the
            # tunnel's / HTTP client's on-401 retry drive the next mint.
            return self._still_valid_cached_token(now)
        self._cached_token = token
        self._cached_expires_at = expires_at
        # Promote the minted JWT to proxy bearer so subsequent refreshes
        # through the Apps proxy are authenticated with a valid credential.
        self._proxy_bearer = token
        return token

    def _still_valid_cached_token(self, now: float) -> str | None:
        """Return the cached token if it hasn't expired outright.

        :param now: Current epoch seconds.
        :returns: The cached token while still valid, else ``None``.
        """
        if self._cached_token is not None and now < self._cached_expires_at:
            return self._cached_token
        return None


def _mint_managed_owner_token(
    mint_url: str,
    server_url: str,
    binding_token: str,
    *,
    proxy_bearer: str | None = None,
) -> tuple[str, float]:
    """Mint one managed-runner owner JWT from the server.

    :param mint_url: Fully-qualified ``/v1/runners/{id}/token`` URL.
    :param server_url: Server base URL, used for the Databricks workspace
        routing header (``X-Databricks-Org-Id``) when applicable.
    :param binding_token: The runner's tunnel binding token, sent as the
        ``X-Omnigent-Runner-Tunnel-Token`` header to authenticate the mint.
    :param proxy_bearer: Optional bearer for a Databricks Apps proxy sitting
        in front of the Omnigent server. The proxy requires a valid
        ``Authorization`` header even on unauthenticated-to-Omnigent
        endpoints; the binding token alone is not enough to pass it.
    :returns: ``(jwt, expires_at_epoch_seconds)``.
    :raises httpx.HTTPError: On network failure or a non-2xx response.
    :raises KeyError: If the response is missing the expected fields.
    """
    from omnigent_client._http import is_loopback_url

    from omnigent.cli_auth import databricks_request_headers
    from omnigent.runner.identity import (
        OMNIGENT_INTERNAL_WS_ORIGIN,
        RUNNER_TUNNEL_TOKEN_HEADER,
    )

    headers = {
        "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
        RUNNER_TUNNEL_TOKEN_HEADER: binding_token,
        **databricks_request_headers(server_url, bearer_token=proxy_bearer),
    }
    with httpx.Client(timeout=10.0, trust_env=not is_loopback_url(server_url)) as client:
        response = client.post(mint_url, headers=headers)
        response.raise_for_status()
        payload = response.json()
    return payload["token"], float(payload["expires_at"])


def _runner_tunnel_binding_token_from_env() -> str | None:
    """Return the optional tunnel binding token from the environment.

    :returns: Secret token used to bind the WebSocket tunnel to its
        runner id, or ``None`` when the runner was started without
        per-tunnel binding.
    :raises RuntimeError: If the token env var is set but empty.
    """
    from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR

    token = os.environ.get(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR)
    if token is None:
        return None
    if not token.strip():
        raise RuntimeError(f"{RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR} must not be empty")
    return token.strip()


def _runner_parent_pid_from_env() -> int | None:
    """Return the optional parent process id from the environment.

    CLI-spawned runners receive the server/CLI process id so they can
    exit when the owning process disappears. Manually started runners
    may omit this value and rely on signals for shutdown.

    :returns: Parent process id, e.g. ``12345``, or ``None`` when no
        parent watchdog should run.
    :raises RuntimeError: If the configured parent pid is empty,
        non-integer, or not positive.
    """
    from omnigent.runner.identity import RUNNER_PARENT_PID_ENV_VAR

    raw_parent_pid = os.environ.get(RUNNER_PARENT_PID_ENV_VAR)
    if raw_parent_pid is None:
        return None
    stripped = raw_parent_pid.strip()
    if not stripped:
        raise RuntimeError(f"{RUNNER_PARENT_PID_ENV_VAR} must not be empty")
    try:
        parent_pid = int(stripped)
    except ValueError as exc:
        raise RuntimeError(f"{RUNNER_PARENT_PID_ENV_VAR} must be an integer") from exc
    if parent_pid <= 0:
        raise RuntimeError(f"{RUNNER_PARENT_PID_ENV_VAR} must be a positive integer")
    return parent_pid


def _parent_process_is_alive(parent_pid: int) -> bool:
    """Return whether an OS process id is still alive.

    :param parent_pid: Parent process id, e.g. ``12345``.
    :returns: ``True`` when the process exists or is not visible due
        to permissions, otherwise ``False``.
    """
    # Not ``os.kill(parent_pid, 0)``: on Windows that maps to TerminateProcess
    # and would kill the parent rather than probe it.
    return _proc.process_alive(parent_pid)


def _parent_is_orphaned(parent_pid: int) -> bool:
    """Return whether this process has been orphaned by *parent_pid*.

    The runner is launched as a direct child of ``parent_pid``, so on POSIX
    ``getppid()`` equals it until the parent dies — at which point the OS
    reparents us to init / a subreaper and ``getppid()`` changes. That
    reparent signal is immune to PID reuse, which can otherwise make the
    liveness probe succeed against an unrelated process that recycled the
    dead parent's pid (seen on busy CI hosts).

    Windows has no reparenting, AND ``os.getppid()`` is unreliable there: the
    interpreter launcher in a venv breaks the parent link, so ``getppid()``
    does not match the spawning process. Using it would report the runner
    orphaned the instant it starts, tearing it down immediately. So on Windows
    rely solely on an explicit liveness probe of the passed-in ``parent_pid``.

    :param parent_pid: The launcher's process id, e.g. ``12345``.
    :returns: ``True`` once the parent is gone, otherwise ``False``.
    """
    if not IS_WINDOWS and os.getppid() != parent_pid:
        return True
    return not _parent_process_is_alive(parent_pid)


def _run_parent_death_killer(
    parent_pid: int,
    request_shutdown: Callable[[], None],
    *,
    adopted: threading.Event | None = None,
    poll_interval_s: float = 0.5,
    grace_s: float = 2.0,
    exit_fn: Callable[[int], None] = os._exit,
) -> None:
    """Force the runner to exit once its parent (host daemon) dies.

    Runs on a dedicated daemon thread, NOT the event loop: when the parent
    dies while a harness subprocess is mid-boot, the runner's own teardown
    removes the harness instance dir out from under it and the asyncio
    shutdown wedges the event loop — so an event-loop watchdog would never
    fire and the WS tunnel would stay open, leaving the server seeing the
    runner online forever. On detecting the parent's death this requests a
    graceful shutdown, then after *grace_s* hard-exits as a backstop. When
    graceful shutdown wins the race the process is already gone and this
    daemon thread dies with it, so the hard exit is a no-op in practice.

    When *adopted* is set the watch ends without tearing the runner down:
    the launcher (CLI) intentionally exited — e.g. the user detached from
    tmux — and wants this runner to keep serving the web UI.
    The CLI sets it (via the adopt signal) while it is still alive, so the
    flag is observed before the subsequent parent-death is detected.

    :param parent_pid: The launcher's process id to monitor, e.g.
        ``12345``.
    :param request_shutdown: Callback that triggers a graceful shutdown
        (e.g. setting the runner's stop event on the loop thread).
    :param adopted: Event set when the runner has been adopted; once set,
        the watcher returns without requesting shutdown. ``None`` disables
        adoption (watch until parent death).
    :param poll_interval_s: Seconds between parent-liveness probes, e.g.
        ``0.5``.
    :param grace_s: Seconds to allow graceful shutdown before the hard
        exit, e.g. ``2.0``.
    :param exit_fn: Hard-exit function, defaults to :func:`os._exit`;
        injectable so tests can observe it without killing the runner.
    :returns: None.
    """
    while not _parent_is_orphaned(parent_pid):
        if adopted is not None and adopted.is_set():
            return
        time.sleep(poll_interval_s)
    # Parent is gone (or never set). Honor a late adopt that raced the
    # parent's exit so an intentional detach is never torn down.
    if adopted is not None and adopted.is_set():
        return
    request_shutdown()
    time.sleep(grace_s)
    # The hard exit is a backstop reached only when graceful shutdown did not
    # finish within the grace window; record it since os._exit skips the
    # exit-reason logging in _run_tunnel_from_env's finally block. The file
    # handler flushes per record, so this lands even though os._exit follows.
    _logger.warning(
        "runner exiting: parent process died and graceful shutdown did not "
        "complete within %.1fs; forcing hard exit",
        grace_s,
    )
    # os._exit skips buffer flushing, so flush logs first for diagnosability.
    with contextlib.suppress(Exception):
        sys.stderr.flush()
    exit_fn(0)


def _runner_workspace_from_env() -> Path | None:
    """Return the optional CLI launch workspace from runner process wiring.

    :returns: The absolute local workspace path passed by the CLI,
        or ``None`` when this runner was launched without workspace
        affinity.
    :raises RuntimeError: If the workspace env var is set but empty.
    """
    from omnigent.runner.identity import RUNNER_WORKSPACE_ENV_VAR

    raw = os.environ.get(RUNNER_WORKSPACE_ENV_VAR)
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        raise RuntimeError(f"{RUNNER_WORKSPACE_ENV_VAR} must not be empty")
    return Path(stripped).expanduser().resolve()


def _runner_isolate_session_from_env() -> bool:
    """Return ``True`` when ``OMNIGENT_RUNNER_ISOLATE_SESSION`` is ``"1"``.

    See :data:`RUNNER_ISOLATE_SESSION_ENV_VAR` for the contract.
    """
    from omnigent.runner.identity import RUNNER_ISOLATE_SESSION_ENV_VAR

    return os.environ.get(RUNNER_ISOLATE_SESSION_ENV_VAR, "").strip() == "1"


def _agent_cache_dest(spec_cache_root: Path, agent_id: str, version: str) -> Path:
    """
    Compute the cache directory for an agent bundle, contained to the root.

    ``agent_id`` and the version header are server-provided but treated as
    untrusted path components: path separators are stripped and the result is
    verified to stay within *spec_cache_root* so a crafted id/version cannot
    traverse out of the cache root (defense-in-depth against path injection).

    :param spec_cache_root: Runner-local cache root for extracted bundles,
        e.g. ``Path("/tmp/runner-specs-xyz")``.
    :param agent_id: Opaque agent identifier, e.g. ``"ag_abc123"``.
    :param version: Bundle version from the ``X-Agent-Version`` header,
        e.g. ``"3"`` (defaults to ``"0"`` when the header is absent).
    :returns: The resolved cache directory, guaranteed inside
        *spec_cache_root*.
    :raises RuntimeError: If the computed path escapes *spec_cache_root*.
    """
    cache_key = f"{agent_id}-v{version}".replace("/", "_").replace("\\", "_")
    cache_root = spec_cache_root.resolve()
    dest = (cache_root / cache_key).resolve()
    if not dest.is_relative_to(cache_root):
        raise RuntimeError(f"spec_resolver: unsafe agent cache path for {agent_id!r}")
    return dest


async def _resolve_agent_spec_from_server(
    server_client: httpx.AsyncClient,
    spec_cache_root: Path,
    agent_id: str,
    session_id: str | None = None,
) -> ResolvedSpec | None:
    """
    Fetch, cache, and parse one agent spec bundle from the Omnigent server.

    :param server_client: HTTP client pointed at the Omnigent server,
        e.g. base URL ``"http://127.0.0.1:6767"``.
    :param spec_cache_root: Stable runner-local cache root for
        extracted agent bundles.
    :param agent_id: Opaque agent identifier to fetch, e.g.
        ``"ag_abc123"``.
    :param session_id: Session identifier used to fetch the bundle
        via the session-scoped endpoint, e.g. ``"conv_abc123"``.
        ``None`` means the runner cannot resolve the session-scoped
        bundle and returns ``None``.
    :returns: The parsed :class:`AgentSpec` plus its extracted bundle
        directory, or ``None`` when the server returns 404 for the
        requested agent.
    :raises RuntimeError: If the server returns a non-200 status
        other than 404.
    """
    from omnigent.runner.native import ResolvedSpec
    from omnigent.spec import load

    if session_id is None:
        _logger.warning(
            "spec_resolver called without session_id for agent %s; "
            "cannot resolve without session context",
            agent_id,
        )
        return None
    path = f"/v1/sessions/{session_id}/agent/contents"
    resp = await server_client.get(path)
    if resp.status_code == 404:
        _logger.info(
            "spec_resolver: GET %s returned 404 for missing agent",
            path,
        )
        return None
    if resp.status_code != 200:
        raise RuntimeError(f"spec_resolver: GET {path} failed with HTTP {resp.status_code}")
    # Env-expansion decision: the MCP/LLM
    # connection actually opens here on the runner, so the runner —
    # not just the server — must refuse to expand ${VAR} against its
    # process env for tenant-supplied (session-scoped) bundles. The
    # server reports provenance via X-Agent-Session-Scoped. Fail safe:
    # a missing/unknown header is treated as session-scoped (no
    # expansion). Only operator-authored template agents expand.
    session_scoped_header = resp.headers.get("X-Agent-Session-Scoped", "true").strip().lower()
    expand_env = session_scoped_header == "false"
    # Cache key: agent id + version header. Re-extracting on
    # every dispatch would be wasteful; keying by version means
    # PUT-induced bundle bumps invalidate naturally.
    version = resp.headers.get("X-Agent-Version", "0")
    dest = _agent_cache_dest(spec_cache_root, agent_id, version)
    # prune_invalid_sub_agents: the server already validated this bundle
    # before serving it, so a sub-agent that fails validation *here* means
    # this runner is older than that server and can't run that sub-agent
    # (e.g. it names a harness this version doesn't know). Drop the
    # unsupported sub-agent and launch the parent with what this runner
    # *does* support, rather than failing every dispatch of the agent.
    # See omnigent.spec.load.
    if not dest.exists():
        dest.mkdir(parents=True)
        load(resp.content, dest=dest, expand_env=expand_env, prune_invalid_sub_agents=True)
    spec = load(dest, expand_env=expand_env, prune_invalid_sub_agents=True)
    return ResolvedSpec(spec=spec, workdir=dest)


def create_app(
    auth_token_factory: Callable[[], str | None] | None = None,
) -> FastAPI:
    """Factory for the runner FastAPI app exposing the harness-contract subset.

    :param auth_token_factory: Pre-built server bearer factory to reuse for the
        HTTP client and native terminal helpers, e.g. the delegated factory
        ``_run_tunnel_from_env`` already built for the WS tunnel. When ``None``,
        the app builds its own.
    :returns: A runner FastAPI app exposing the harness-contract subset.
    """
    from omnigent.cli_auth import open_server_client
    from omnigent.runner.app import create_runner_app
    from omnigent.runner.identity import (
        OMNIGENT_INTERNAL_WS_ORIGIN,
        OMNIGENT_SESSION_ENV_VALUE,
        OMNIGENT_SESSION_ENV_VAR,
        RUNNER_ID_ENV_VAR,
        RUNNER_TUNNEL_TOKEN_HEADER,
        get_stable_runner_id,
    )
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    server_url = _server_url_from_env()
    runner_workspace = _runner_workspace_from_env()
    isolate_session = _runner_isolate_session_from_env()
    # Stable runner UUID, persisted so resume works across
    # restarts (§5 "Persistence" in RUNNER.md).
    _runner_id = get_stable_runner_id()
    os.environ[RUNNER_ID_ENV_VAR] = _runner_id
    # Stamp the Omnigent session marker into the runner's environment so
    # every process this runner spawns can detect it is running inside an
    # Omnigent agent session, the way Claude Code sets CLAUDE_CODE and
    # Codex sets CODEX. Harness workers inherit it (the process manager
    # merges os.environ), native CLI terminals copy os.environ, and the
    # claude-sdk SDK merges os.environ. The deny-by-default env scrubbers
    # (os_env, codex, pi) allowlist it so it survives their scrub.
    os.environ[OMNIGENT_SESSION_ENV_VAR] = OMNIGENT_SESSION_ENV_VALUE

    # Keep the harness manager on its default /tmp/omnigent root.
    # Nesting harness UDS paths under caller-provided temp dirs can
    # exceed AF_UNIX path limits on macOS.
    pm = HarnessProcessManager()

    # MCP pool — the runner owns stdio MCP subprocess spawning.
    # The Omnigent server's POST /v1/sessions/{id}/mcp handles policy
    # evaluation and delegates execution here via
    # POST /v1/sessions/{id}/mcp/execute (tunneled through the WS
    # tunnel the runner opened to the Omnigent server at startup).
    # stdio_cwd=runner_workspace ensures relative command paths like
    # ".venv/bin/python" resolve against the user's project root.

    from omnigent.runner.mcp_manager import RunnerMcpManager

    # Reuse the caller's factory when given (shares one resolved SDK auth +
    # token cache); otherwise build our own.
    if auth_token_factory is None:
        auth_token_factory = _make_auth_token_factory()
    binding_token = _runner_tunnel_binding_token_from_env()
    server_client = open_server_client(
        server_url,
        auth=_RunnerDatabricksAuth(auth_token_factory),
        # Announce the runner as a first-party non-browser client via the
        # sentinel Origin. The server's require_trusted_origin CSRF guard on
        # the multipart routes (POST /v1/sessions bundle create, file upload
        # — both reached from tool_dispatch over this client) requires a
        # trusted Origin; the runner sends none otherwise, so the sentinel is
        # what lets sys_session_create / sys_upload_file through. The factory
        # folds the routing/slice-key headers in on top; we pass only the
        # runner's tunnel-binding token (when present) alongside the Origin.
        headers={
            "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
            **({RUNNER_TUNNEL_TOKEN_HEADER: binding_token} if binding_token is not None else {}),
        },
        timeout=httpx.Timeout(5.0, read=None),
        # NOTE: ``follow_redirects`` deliberately stays False.
        # ``_RunnerDatabricksAuth.auth_flow`` needs to *see* the
        # Databricks Apps OAuth login redirect (302 →
        # ``/oidc/...authorize``) to know it should re-mint the bearer
        # and retry the original POST. With ``follow_redirects=True``,
        # httpx walks the redirect chain inside the auth loop and
        # hands the auth flow only the terminal HTML login page,
        # defeating the retry. See ``_is_login_redirect_or_unauthorized``.
        follow_redirects=False,
    )

    mcp_manager = RunnerMcpManager(
        stdio_cwd=runner_workspace,
        server_client=server_client,
    )

    # Stable extraction root for spec_resolver; bundles get
    # extracted under here keyed by agent id + version. Lives for
    # the runner's lifetime (cleaned up in the shutdown handler
    # below) so AgentSpec path references (skills, bundled files,
    # etc.) stay valid after spec_resolver returns. Earlier impl
    # used ``tempfile.TemporaryDirectory()`` inside spec_resolver,
    # which deleted the dir on return, leaving downstream tools
    # (terminal dispatch, skill resolution) holding broken Path
    # references.
    import tempfile

    _spec_cache_root = Path(tempfile.mkdtemp(prefix=f"runner-specs-{_runner_id}-"))

    async def spec_resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec | None:
        """
        Fetch agent spec from the Omnigent server, extract under the
        runner's stable spec cache, and return the parsed
        :class:`AgentSpec`.

        When *session_id* is provided, uses the session-scoped
        ``GET /v1/sessions/{id}/agent/contents`` endpoint. Falls
        back to ``GET /api/agents/{id}/contents`` when session_id
        is ``None``.

        Returns ``None`` only on 404. Other HTTP statuses and
        failures (network, extraction, parse) raise.

        :param agent_id: Opaque agent identifier from the
            response-create body.
        :param session_id: Session identifier, e.g.
            ``"conv_abc123"``. ``None`` for legacy callers.
        :returns: The parsed :class:`AgentSpec`, or ``None`` if
            the server doesn't have the agent.
        """
        return await _resolve_agent_spec_from_server(
            server_client,
            _spec_cache_root,
            agent_id,
            session_id=session_id,
        )

    # Out-of-process runner owns its own TerminalRegistry.
    from omnigent.inner.terminal import reap_orphaned_terminals
    from omnigent.terminals import TerminalRegistry

    _terminal_registry = TerminalRegistry(conversation_link_base_url=server_url)
    # Reap terminal tmux servers leaked by a previous runner that died
    # without graceful shutdown (SIGKILL / harness teardown). Detached
    # tmux outlives its supervisor, and runner-bound SDK sessions now
    # auto-create the embedded REPL terminal — without this sweep every
    # ungraceful exit leaks one tmux server per session (enough to
    # starve CI hosts running many short-lived runners).
    _reaped_terminals = reap_orphaned_terminals()
    if _reaped_terminals:
        _logger.info(
            "Reaped %d orphaned terminal tmux server(s) from prior runs",
            _reaped_terminals,
        )

    # Reuse the tunnel binding token for runner-side request auth.
    # The same secret is already shared between the
    # CLI launcher and this runner process via env var.
    runner_auth_token = binding_token

    app = create_runner_app(
        process_manager=pm,
        spec_resolver=spec_resolver,
        server_client=server_client,
        terminal_registry=_terminal_registry,
        runner_workspace=runner_workspace,
        per_session_workspace=isolate_session,
        mcp_manager=mcp_manager,
        auth_token=runner_auth_token,
        auth_token_factory=auth_token_factory,
    )

    async def _start_pm() -> None:
        """Start harness process manager; register MCP prewarm metadata if requested."""
        await pm.start()
        prewarm_path = os.environ.get(_RUNNER_PREWARM_SPEC_PATH_ENV_VAR)
        if prewarm_path and mcp_manager is not None:
            try:
                from omnigent.spec import load as _load_spec

                # The prewarm spec is a local operator-provided path
                # (set by the CLI local-runner spawn), so it is trusted
                # and ${VAR} expands against the operator env — unlike
                # tenant session-scoped bundles resolved from the server.
                prewarm_spec = _load_spec(Path(prewarm_path), expand_env=True)
                await mcp_manager.prewarm(prewarm_spec)
                _logger.info(
                    "runner MCP prewarm registered for %s (servers=%d)",
                    prewarm_path,
                    len(prewarm_spec.mcp_servers or []),
                )
            except Exception:
                _logger.exception("runner MCP prewarm failed for %s", prewarm_path)
        # Native-pane idle reaper (#1349): reclaims idle native CLI panes.
        _pane_reaper = getattr(app.state, "native_pane_reaper", None)
        if _pane_reaper is not None:
            await _pane_reaper.start()
        # Backstop for a hard host/runner death (SIGKILL / OOM / crash) that
        # ran no graceful teardown: a codex app-server spawned in its own
        # session outlives its runner. Reconciling the crash-safe registry at
        # boot reaps any such orphan whose owner lock is no longer held (its
        # runner is gone), so a fresh runner on the host cleans up what a dead
        # predecessor left. Held owner locks (live sibling runners) are skipped.
        from omnigent.codex_native_process_registry import (
            reconcile_codex_native_process_registry,
        )

        with contextlib.suppress(Exception):
            await asyncio.to_thread(reconcile_codex_native_process_registry)

    async def _stop_pm() -> None:
        """Stop runner-owned resources for graceful process exit.

        :returns: None.
        """
        _pane_reaper = getattr(app.state, "native_pane_reaper", None)
        if _pane_reaper is not None:
            await _pane_reaper.shutdown()
        # Close host-spawned codex-native app-servers before the process exits.
        # Each is spawned in its own session (survives the runner's death), and a
        # host-initiated stop tears the runner down without a per-session DELETE,
        # so without this they orphan as lingering ``codex`` processes.
        from omnigent.runner.native import teardown_all_codex_native_app_servers

        with contextlib.suppress(Exception):
            await teardown_all_codex_native_app_servers()
        await pm.shutdown()
        await _terminal_registry.shutdown()
        if mcp_manager is not None:
            await mcp_manager.shutdown()
        await server_client.aclose()
        # Best-effort cleanup of extracted spec bundles. Missing
        # / already-gone is fine; ignore_errors handles a partial
        # write that leaves a directory in an unreadable state.
        import shutil

        shutil.rmtree(_spec_cache_root, ignore_errors=True)

    # starlette 1.x removed add_event_handler; drive startup/shutdown via lifespan.
    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await _start_pm()
        try:
            yield
        finally:
            await _stop_pm()

    app.router.lifespan_context = _lifespan
    return app


async def _run_tunnel_from_env() -> None:
    """Run the runner as a WebSocket tunnel client.

    :returns: None.
    """
    from concurrent.futures import ThreadPoolExecutor

    from omnigent.runner.identity import get_stable_runner_id
    from omnigent.runner.transports.ws_tunnel.serve import serve_tunnel

    # Bound the asyncio default executor before anything uses it (create_app,
    # telemetry, and every to_thread offload). Setting it here means Python's
    # lazy min(32, cpu+4)-thread default pool is never created.
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(
            max_workers=_runner_threadpool_max_workers(),
            thread_name_prefix="runner",
        )
    )

    server_url = _server_url_from_env()
    auth_token_factory = _make_auth_token_factory()
    # Write the singleton to the canonical module so it is visible to all
    # importers even when this file runs as __main__ (which Python treats as a
    # separate module object from omnigent.runner._entry). Importing the
    # canonical name here ensures sys.modules registers it before any
    # harness code tries to read it.
    import omnigent.runner._entry as _canonical_entry

    _canonical_entry._set_runner_auth_factory(auth_token_factory)
    _set_runner_auth_factory(auth_token_factory)
    auth_token = auth_token_factory() if auth_token_factory is not None else None
    binding_token = _runner_tunnel_binding_token_from_env()
    parent_pid = _runner_parent_pid_from_env()
    runner_id = get_stable_runner_id()

    # Initialize OTel tracing in the runner process so the ExecutorAdapter
    # can emit spans for agent turns, tool calls, and LLM interactions.
    # No-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset.
    try:
        from omnigent.runtime import telemetry

        telemetry.init("omni-runner")
    except Exception:  # noqa: BLE001 — best-effort; tracing failure must not crash the runner
        _logger.debug("telemetry init failed in runner", exc_info=True)

    # Reuse the tunnel's token factory for the app's httpx client so the
    # runner resolves Databricks auth once at boot, not twice.
    app = create_app(auth_token_factory=auth_token_factory)
    idle_timeout_s = _load_runner_idle_timeout_s_from_config()
    # starlette 1.x removed Router.startup/shutdown; drive the lifespan manually.
    _lifespan_cm = app.router.lifespan_context(app)
    await _lifespan_cm.__aenter__()
    # Move the now-static import graph out of GC's tracked set: cheaper cyclic
    # collections and fewer dirtied pages over the runner's life.
    gc.freeze()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    last_activity_at = loop.time()

    def _mark_activity() -> None:
        """Record real runner work for the inactivity watchdog.

        Called by the WebSocket tunnel frame dispatcher for non-keepalive
        request frames.

        :returns: None.
        """
        nonlocal last_activity_at
        last_activity_at = loop.time()

    def _last_activity() -> float:
        """Return the last real runner activity time.

        :returns: Monotonic timestamp from the runner event loop.
        """
        return last_activity_at

    def _has_active_work() -> bool:
        """Return whether the runner is currently executing agent work.

        :returns: ``True`` while at least one agent turn is active.
        """
        callback = getattr(app.state, "has_active_work", None)
        if not callable(callback):
            return False
        return bool(callback())

    # Human-readable reason for why the runner is shutting down, recorded
    # by whichever path wins the shutdown race and logged on the way out so
    # the runner log explains the exit instead of just stopping.
    exit_reason: str | None = None

    def _record_exit_reason(reason: str) -> None:
        """Record the first observed shutdown reason.

        :param reason: Human-readable cause, e.g. ``"received SIGTERM"``.
        :returns: None.
        """
        nonlocal exit_reason
        if exit_reason is None:
            exit_reason = reason

    # Set when the launcher adopts this runner (tmux detach); makes the
    # parent-death killer stand down so the runner outlives the CLI.
    adopted_event = threading.Event()
    _install_signal_handlers(
        stop_event,
        adopted_event=adopted_event,
        record_reason=_record_exit_reason,
    )
    # Set (instead of stop_event) on an idle-reaper shutdown so the tunnel
    # drains its session streams and closes cleanly — the server then sees an
    # end-of-stream, not an abrupt drop that renders a scary error banner.
    tunnel_shutdown_event = asyncio.Event()
    tunnel_task = asyncio.create_task(
        serve_tunnel(
            cast("_ASGIApp", app),  # FastAPI is ASGI-compatible; cast narrows for mypy
            server_url=server_url,
            runner_id=runner_id,
            runner_version=_RUNNER_VERSION,
            auth_token=auth_token,
            tunnel_token=binding_token,
            auth_token_factory=auth_token_factory,
            on_reconnect=getattr(app.state, "catch_up_scan", None),
            on_activity=_mark_activity,
            shutdown_event=tunnel_shutdown_event,
            on_graceful_shutdown=getattr(app.state, "drain_session_streams", None),
        ),
        name=f"runner-ws-tunnel:{runner_id}",
    )
    stop_task = asyncio.create_task(stop_event.wait(), name="runner-signal-wait")
    idle_task: asyncio.Task[None] | None = None
    if idle_timeout_s > 0:

        def _request_idle_shutdown() -> None:
            """Attribute the exit to the idle watchdog, then drain gracefully.

            Signals the tunnel (not stop_event) so it flushes each session
            stream's ``[DONE]`` and completes its close handshake before the
            process exits; the tunnel task then returns on its own and the main
            wait below unwinds. Does NOT set stop_event — that would trip the
            abrupt-teardown path (cancel the tunnel mid-drain).

            :returns: None.
            """
            _record_exit_reason("idle timeout reached")
            tunnel_shutdown_event.set()

        idle_task = asyncio.create_task(
            _run_inactivity_monitor(
                idle_timeout_s=idle_timeout_s,
                get_last_activity=_last_activity,
                has_active_work=_has_active_work,
                request_shutdown=_request_idle_shutdown,
            ),
            name=f"runner-idle-monitor:{runner_id}",
        )
    if parent_pid is not None:

        def _request_parent_death_shutdown() -> None:
            """Attribute the exit to parent death, then stop on the loop.

            Invoked from the parent-death daemon thread, so the reason is
            recorded and the stop event set via the event loop.

            :returns: None.
            """
            _record_exit_reason("parent process died")
            loop.call_soon_threadsafe(stop_event.set)

        # Orphan guard runs on a dedicated daemon thread, not the event
        # loop: if the loop wedges during shutdown (harness mid-boot when
        # the host dies), an event-loop watchdog could never fire. The
        # thread requests graceful shutdown via the loop, then hard-exits
        # as a backstop. See _run_parent_death_killer.
        threading.Thread(
            target=_run_parent_death_killer,
            args=(parent_pid, _request_parent_death_shutdown),
            kwargs={"adopted": adopted_event},
            name=f"runner-parent-killer:{parent_pid}",
            daemon=True,
        ).start()
    wait_tasks = {tunnel_task, stop_task}
    if idle_task is not None:
        wait_tasks.add(idle_task)
    try:
        done, _ = await asyncio.wait(
            wait_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if tunnel_task in done:
            # The tunnel returning first means the WS connection ended on its
            # own rather than a signal/idle/parent-death shutdown request.
            _record_exit_reason("websocket tunnel closed")
            await tunnel_task
        elif idle_task is not None and idle_task in done and tunnel_shutdown_event.is_set():
            # Idle reaper fired: it signalled the tunnel to drain its session
            # streams and close cleanly, then the monitor returned (so idle_task
            # is done). Wait for the tunnel to finish that graceful close —
            # bounded — instead of falling straight into the finally, which
            # would cancel it mid-drain and turn the clean end-of-stream back
            # into an abrupt drop. A tunnel that overruns the budget is
            # cancelled by the finally as a backstop.
            await asyncio.wait({tunnel_task}, timeout=_GRACEFUL_SHUTDOWN_TUNNEL_TIMEOUT_S)
    finally:
        # Log why the runner is stopping so the runner log explains the exit.
        # A crash unwinding through here is attributed by sys.excepthook with
        # its traceback, so only record the reason for an orderly shutdown.
        if sys.exc_info()[0] is None:
            _logger.info("runner exiting: %s", exit_reason or "shutdown requested")
        for task in wait_tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        with contextlib.suppress(asyncio.CancelledError):
            await tunnel_task
        if idle_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await idle_task
        await _lifespan_cm.__aexit__(None, None, None)


def _install_signal_handlers(
    stop_event: asyncio.Event,
    adopted_event: threading.Event | None = None,
    record_reason: Callable[[str], None] | None = None,
) -> None:
    """Install process signal handlers that request graceful shutdown.

    :param stop_event: Event set when SIGINT or SIGTERM arrives.
    :param adopted_event: Optional event set when
        :data:`RUNNER_ADOPT_SIGNAL` arrives, telling the parent-death
        killer to stand down so the runner survives an intentional CLI
        exit (tmux detach). ``None`` skips the handler.
    :param record_reason: Optional callback given the signal name when a
        shutdown signal arrives, so the exit log line can attribute the
        cause. ``None`` skips attribution.
    :returns: None.
    """
    loop = asyncio.get_running_loop()

    def _handle_shutdown_signal(sig: int) -> None:
        """Record the triggering signal and request graceful shutdown.

        :param sig: The delivered signal number, e.g. ``signal.SIGTERM``.
        :returns: None.
        """
        if record_reason is not None:
            record_reason(f"received {signal.Signals(sig).name}")
        stop_event.set()

    degraded = False

    def _try_add_handler(sig: int, callback: Callable[..., None], *args: object) -> None:
        """Register one handler; degrade instead of crashing the runner.

        ``add_signal_handler`` raises RuntimeError when the loop's signal
        wakeup fd is unusable — observed in zygote-forked runners whose
        wakeup fd ended up in blocking mode ("the fd N must be in
        non-blocking mode"), crash-looping every fork of the session.
        Graceful-shutdown handlers are a nicety: a runner without them
        still serves sessions and still exits via the parent-death
        backstop, so warn once and continue instead of dying.

        :param sig: Signal number to register.
        :param callback: Handler invoked when the signal arrives.
        :param args: Positional arguments passed to ``callback``.
        :returns: None.
        """
        nonlocal degraded
        if degraded:
            return
        try:
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, callback, *args)
        except RuntimeError:
            degraded = True
            _logger.warning(
                "Signal handler registration failed; continuing without "
                "graceful-shutdown signal handling",
                exc_info=True,
            )

    for sig in (signal.SIGINT, signal.SIGTERM):
        _try_add_handler(sig, _handle_shutdown_signal, sig)
    if adopted_event is not None:
        from omnigent.runner.identity import RUNNER_ADOPT_SIGNAL

        if RUNNER_ADOPT_SIGNAL is None:
            return
        _try_add_handler(RUNNER_ADOPT_SIGNAL, adopted_event.set)


def _install_crash_logging() -> None:
    """Log the exit reason for otherwise-silent runner crashes.

    Chains a ``sys.excepthook`` that records any uncaught exception (the
    "randomly dying" case) to the runner log before the interpreter
    prints its traceback and exits. Signals, idle timeout, and tunnel
    drops are attributed at their own sites; this covers the crashes that
    would otherwise leave only a bare traceback on stderr.

    Note: neither this hook nor ``atexit`` fires on ``os._exit`` (the
    parent-death backstop, which logs its own reason) or on ``SIGKILL``.
    Idempotent: a second call is a no-op so repeated installs never stack.

    :returns: None.
    """
    previous_hook = sys.excepthook
    if getattr(previous_hook, "_omnigent_runner_crash_hook", False):
        return

    def _log_uncaught(
        exc_type: type[BaseException],
        exc: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        """Record an uncaught exception, then defer to the prior hook.

        :param exc_type: The exception class, e.g. ``RuntimeError``.
        :param exc: The exception instance.
        :param traceback: The associated traceback object.
        :returns: None.
        """
        with contextlib.suppress(Exception):
            _logger.critical(
                "runner exiting: uncaught %s: %s",
                exc_type.__name__,
                exc,
                exc_info=(exc_type, exc, traceback),
            )
        previous_hook(exc_type, exc, traceback)

    _log_uncaught._omnigent_runner_crash_hook = True  # type: ignore[attr-defined]
    sys.excepthook = _log_uncaught


def _maybe_prewarm_ambient_detection() -> None:
    """Prewarm ambient provider detection for claude-native launches.

    Terminal auto-create resolves the session's provider config, and on a
    machine with no explicit provider that runs ambient detection — on macOS
    a ``claude auth status`` subprocess (~0.6-0.9s) — inside the
    user-visible "Starting up…" window. Starting the sweep at boot overlaps
    it with tunnel connect and session init instead.

    Gated on the launch-harness stamp the host injects (absent for CLI-local
    runners and hosts that predate it), so other harnesses don't pay a
    speculative subprocess on every launch.
    """
    from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT
    from omnigent.runner.identity import RUNNER_LAUNCH_HARNESS_ENV_VAR

    if os.environ.get(RUNNER_LAUNCH_HARNESS_ENV_VAR) != CLAUDE_NATIVE_CODING_AGENT.harness:
        return
    from omnigent.onboarding.ambient import prewarm_detect_providers

    prewarm_detect_providers()


def main() -> None:
    """Console entry point for the runner tunnel process.

    :returns: None.
    """
    from omnigent.process_logging import configure_process_logging

    # Spawned with -P, so the workspace is not on sys.path. Re-add it now that
    # the real omnigent is imported (it can no longer be shadowed) so
    # spec-declared local tools living in the workspace still import.
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    configure_process_logging("runner", force=True)
    _install_crash_logging()
    _maybe_prewarm_ambient_detection()
    try:
        asyncio.run(_run_tunnel_from_env())
    except RuntimeError as exc:
        if not str(exc).startswith(RUNNER_TUNNEL_REJECTION_PREFIX):
            raise
        # A fatal server rejection is an expected, actionable exit — log the
        # reason but keep stderr to the concise message (no traceback).
        _logger.error("runner exiting: %s", exc)
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
