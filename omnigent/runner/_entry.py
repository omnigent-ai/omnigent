"""Runner subprocess entry point.

Launched by the CLI when spawning the runner as a separate process.
Reads process wiring from environment variables set by the parent:
- ``RUNNER_SERVER_URL``: Omnigent server base URL for outbound calls
  (spec fetch, response resolution, and WS tunnel registration).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
from fastapi import FastAPI

from omnigent.runner.transports.ws_tunnel.serve import RUNNER_TUNNEL_REJECTION_PREFIX

if TYPE_CHECKING:
    from omnigent.runner.app import ResolvedSpec
    from omnigent.runner.transports.ws_tunnel.serve import _ASGIApp

_RUNNER_SERVER_URL_ENV_VAR = "RUNNER_SERVER_URL"
_RUNNER_PREWARM_SPEC_PATH_ENV_VAR = "RUNNER_PREWARM_SPEC_PATH"
_RUNNER_VERSION = "0.1.0"
_RUNNER_CONFIG_HOME_ENV_VAR = "OMNIGENT_CONFIG_HOME"
_DEFAULT_RUNNER_IDLE_TIMEOUT_S = 60 * 60
_RUNNER_IDLE_MONITOR_MAX_POLL_INTERVAL_S = 60.0
_logger = logging.getLogger(__name__)


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
    :param has_active_work: Callback returning whether any agent turn is
        currently running.
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

    def __init__(self, factory: Callable[[], str | None] | None) -> None:
        """
        :param factory: Sync callable that returns a fresh bearer
            token, e.g. the return value of
            :func:`_make_auth_token_factory`. ``None`` disables
            auth (local unauthenticated servers).
        """
        self._factory = factory

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
        if self._factory is not None:
            token = self._factory()
            if not token:
                raise httpx.RequestError("Databricks token refresh returned no token")
            request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if self._factory is None:
            return
        if _is_login_redirect_or_unauthorized(response):
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
    if response.status_code == 401:
        return True
    if not response.is_redirect:
        return False
    location = response.headers.get("location", "")
    # Match the Apps front-door OAuth flow specifically. ``/oidc/`` is
    # the OAuth provider mount; ``/.auth/`` covers the callback path
    # (e.g. ``/.auth/callback``) the Apps proxy uses when stitching the
    # browser-style flow back together.
    return "/oidc/" in location or "/.auth/" in location


def _make_auth_token_factory(
    server_url: str | None = None,
) -> Callable[[], str | None] | None:
    """Build a callable that mints fresh auth tokens.

    Resolution order:
      1. Stored OIDC token from ``~/.omnigent/auth_tokens.json``
         (populated by ``omnigent login``), keyed by ``server_url``.
      2. Databricks OAuth token (refreshed via the SDK) — host-keyed
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
    from omnigent.inner.databricks_executor import (
        DatabricksAuthError,
        _DatabricksBearerAuth,
        _resolve_databricks_auth,
    )

    resolved_server_url = server_url or os.environ.get(_RUNNER_SERVER_URL_ENV_VAR)

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

    # Probe once to check if credentials are available.
    try:
        if _factory() is not None:
            return _factory
    except (ValueError, OSError, ImportError):
        pass
    return None


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
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parent_is_orphaned(parent_pid: int) -> bool:
    """Return whether this process has been orphaned by *parent_pid*.

    The runner is launched as a direct child of ``parent_pid``, so
    ``getppid()`` equals it until the parent dies — at which point the OS
    reparents us to init / a subreaper and ``getppid()`` changes. That
    reparent signal is immune to PID reuse, which can otherwise make the
    ``os.kill(pid, 0)`` liveness probe succeed against an unrelated process
    that recycled the dead parent's pid (seen on busy CI hosts).

    :param parent_pid: The launcher's process id, e.g. ``12345``.
    :returns: ``True`` once the parent is gone, otherwise ``False``.
    """
    return os.getppid() != parent_pid or not _parent_process_is_alive(parent_pid)


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
    from omnigent.runner.app import ResolvedSpec
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
    if not dest.exists():
        dest.mkdir(parents=True)
        load(resp.content, dest=dest, expand_env=expand_env)
    spec = load(dest, expand_env=expand_env)
    return ResolvedSpec(spec=spec, workdir=dest)


def create_app(
    auth_token_factory: Callable[[], str | None] | None = None,
) -> FastAPI:
    """Factory for the runner FastAPI app exposing the harness-contract subset.

    :param auth_token_factory: Pre-built Databricks token factory to reuse for
        the server httpx client's auth, e.g. the one ``_run_tunnel_from_env``
        already built for the WS tunnel header. When ``None``, the app builds
        its own. Reusing the caller's factory shares one resolved SDK auth (and
        its in-memory token cache) instead of resolving Databricks credentials
        a second time during runner boot.
    :returns: A runner FastAPI app exposing the harness-contract subset.
    """
    from omnigent.runner.app import create_runner_app
    from omnigent.runner.identity import RUNNER_ID_ENV_VAR, get_stable_runner_id
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    server_url = _server_url_from_env()
    runner_workspace = _runner_workspace_from_env()
    isolate_session = _runner_isolate_session_from_env()
    # Stable runner UUID, persisted so resume works across
    # restarts (§5 "Persistence" in RUNNER.md).
    _runner_id = get_stable_runner_id()
    os.environ[RUNNER_ID_ENV_VAR] = _runner_id

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
    server_client = httpx.AsyncClient(
        base_url=server_url,
        auth=_RunnerDatabricksAuth(auth_token_factory),
        timeout=httpx.Timeout(5.0, read=None),
        # NOTE: ``follow_redirects`` deliberately stays False.
        # ``_RunnerDatabricksAuth.auth_flow`` needs to *see* the
        # Databricks Apps OAuth login redirect (302 →
        # ``/oidc/...authorize``) to know it should re-mint the bearer
        # and retry the original POST. With ``follow_redirects=True``,
        # httpx walks the redirect chain inside the auth loop and
        # hands the auth flow only the terminal HTML login page,
        # defeating the retry. See ``_is_login_redirect_or_unauthorized``.
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
    runner_auth_token = _runner_tunnel_binding_token_from_env()

    app = create_runner_app(
        process_manager=pm,
        spec_resolver=spec_resolver,
        server_client=server_client,
        terminal_registry=_terminal_registry,
        runner_workspace=runner_workspace,
        per_session_workspace=isolate_session,
        mcp_manager=mcp_manager,
        auth_token=runner_auth_token,
    )

    async def _start_pm() -> None:
        """Start harness process manager; kick off MCP prewarm if requested."""
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
                    "runner MCP prewarm scheduled for %s (servers=%d)",
                    prewarm_path,
                    len(prewarm_spec.mcp_servers or []),
                )
            except Exception:
                _logger.exception("runner MCP prewarm failed for %s", prewarm_path)

    async def _stop_pm() -> None:
        """Stop runner-owned resources for graceful process exit.

        :returns: None.
        """
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

    app.add_event_handler("startup", _start_pm)
    app.add_event_handler("shutdown", _stop_pm)

    return app


async def _run_tunnel_from_env() -> None:
    """Run the runner as a WebSocket tunnel client.

    :returns: None.
    """
    from omnigent.runner.identity import get_stable_runner_id
    from omnigent.runner.transports.ws_tunnel.serve import serve_tunnel

    server_url = _server_url_from_env()
    auth_token_factory = _make_auth_token_factory()
    auth_token = auth_token_factory() if auth_token_factory is not None else None
    binding_token = _runner_tunnel_binding_token_from_env()
    parent_pid = _runner_parent_pid_from_env()
    runner_id = get_stable_runner_id()

    # Initialize MLflow tracing in the runner process so the
    # ExecutorAdapter can emit spans for agent turns, tool calls,
    # and LLM interactions. No-op when OTEL_EXPORTER_OTLP_ENDPOINT
    # is unset or mlflow is not installed.
    try:
        from omnigent.runtime import telemetry

        telemetry.init()
    except ImportError:
        _logger.debug("telemetry init skipped in runner (mlflow not installed)")
    except Exception:  # noqa: BLE001 — best-effort; tracing failure must not crash the runner
        _logger.debug("telemetry init failed in runner", exc_info=True)

    # Reuse the tunnel's token factory for the app's httpx client so the
    # runner resolves Databricks auth once at boot, not twice.
    app = create_app(auth_token_factory=auth_token_factory)
    idle_timeout_s = _load_runner_idle_timeout_s_from_config()
    await app.router.startup()
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

    # Set when the launcher adopts this runner (tmux detach); makes the
    # parent-death killer stand down so the runner outlives the CLI.
    adopted_event = threading.Event()
    _install_signal_handlers(stop_event, adopted_event=adopted_event)
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
        ),
        name=f"runner-ws-tunnel:{runner_id}",
    )
    stop_task = asyncio.create_task(stop_event.wait(), name="runner-signal-wait")
    idle_task: asyncio.Task[None] | None = None
    if idle_timeout_s > 0:
        idle_task = asyncio.create_task(
            _run_inactivity_monitor(
                idle_timeout_s=idle_timeout_s,
                get_last_activity=_last_activity,
                has_active_work=_has_active_work,
                request_shutdown=stop_event.set,
            ),
            name=f"runner-idle-monitor:{runner_id}",
        )
    if parent_pid is not None:
        # Orphan guard runs on a dedicated daemon thread, not the event
        # loop: if the loop wedges during shutdown (harness mid-boot when
        # the host dies), an event-loop watchdog could never fire. The
        # thread requests graceful shutdown via the loop, then hard-exits
        # as a backstop. See _run_parent_death_killer.
        threading.Thread(
            target=_run_parent_death_killer,
            args=(parent_pid, lambda: loop.call_soon_threadsafe(stop_event.set)),
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
            await tunnel_task
    finally:
        for task in wait_tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        with contextlib.suppress(asyncio.CancelledError):
            await tunnel_task
        if idle_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await idle_task
        await app.router.shutdown()


def _install_signal_handlers(
    stop_event: asyncio.Event,
    adopted_event: threading.Event | None = None,
) -> None:
    """Install process signal handlers that request graceful shutdown.

    :param stop_event: Event set when SIGINT or SIGTERM arrives.
    :param adopted_event: Optional event set when
        :data:`RUNNER_ADOPT_SIGNAL` arrives, telling the parent-death
        killer to stand down so the runner survives an intentional CLI
        exit (tmux detach). ``None`` skips the handler.
    :returns: None.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    if adopted_event is not None:
        from omnigent.runner.identity import RUNNER_ADOPT_SIGNAL

        if RUNNER_ADOPT_SIGNAL is None:
            return
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(RUNNER_ADOPT_SIGNAL, adopted_event.set)


def main() -> None:
    """Console entry point for the runner tunnel process.

    :returns: None.
    """
    log_level = os.environ.get("OMNIGENT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )
    try:
        asyncio.run(_run_tunnel_from_env())
    except RuntimeError as exc:
        if not str(exc).startswith(RUNNER_TUNNEL_REJECTION_PREFIX):
            raise
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
