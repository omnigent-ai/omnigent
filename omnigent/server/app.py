"""FastAPI application — main entry point for the omnigent server."""

import asyncio
import logging
import mimetypes
import os
import tarfile
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from omnigent._platform import resolve_repo_symlink
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.native_coding_agents import (
    ANTIGRAVITY_NATIVE_CODING_AGENT,
    CLAUDE_NATIVE_CODING_AGENT,
    CODEX_NATIVE_CODING_AGENT,
    CURSOR_NATIVE_CODING_AGENT,
    KIMI_NATIVE_CODING_AGENT,
    KIRO_NATIVE_CODING_AGENT,
    OPENCODE_NATIVE_CODING_AGENT,
    PI_NATIVE_CODING_AGENT,
    QWEN_NATIVE_CODING_AGENT,
)
from omnigent.resources import examples as _examples_resources
from omnigent.runtime import (
    get_terminal_registry,
    set_harness_process_manager,
    set_runner_router,
    set_runner_ws_factory,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.server.auth import AuthProvider
from omnigent.server.managed_hosts import ManagedSandboxConfig
from omnigent.server.mcp_pool import ServerMcpPool
from omnigent.server.performance_metrics import (
    ServerMetricsOtelPublisher,
    ServerPerformanceMetrics,
    publish_server_metrics_periodically,
    set_request_duration_for_access_log,
)
from omnigent.server.routes.builtin_agents import create_builtin_agents_router
from omnigent.server.routes.comments import create_comments_router
from omnigent.server.routes.default_policies import create_default_policies_router
from omnigent.server.routes.policy_registry import create_policy_registry_router
from omnigent.server.routes.runner_tunnel import create_runner_tunnel_router
from omnigent.server.routes.session_mcp_servers import create_session_mcp_servers_router
from omnigent.server.routes.session_policies import create_session_policies_router
from omnigent.server.routes.sessions import (
    SessionLiveness,
    create_sessions_router,
    set_server_runner_router,
)
from omnigent.server.routes.terminal_attach import create_terminal_attach_router
from omnigent.server.ws_origin import WebSocketOriginMiddleware
from omnigent.stores import (
    AgentStore,
    ArtifactStore,
    ConversationStore,
    FileStore,
)
from omnigent.stores.comment_store import CommentStore
from omnigent.stores.conversation_store import SessionConnectivity
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.policy_store import PolicyStore

_logger = logging.getLogger(__name__)


def _register_web_mimetypes() -> None:
    """Pin Content-Type for web UI assets regardless of the OS MIME registry.

    Starlette's ``StaticFiles`` derives ``Content-Type`` from
    ``mimetypes.guess_type``. On Windows that consults the registry, where
    ``.js`` is frequently mapped to ``text/plain`` — so the browser refuses to
    execute the SPA's ES modules ("Loading module … was blocked because of a
    disallowed MIME type"). Registering the web types explicitly makes the
    bundled UI serve correctly on every platform and removes the dependency on
    a machine's registry configuration.
    """
    for ext, ctype in (
        (".js", "text/javascript"),
        (".mjs", "text/javascript"),
        (".css", "text/css"),
        (".json", "application/json"),
        (".map", "application/json"),
        (".wasm", "application/wasm"),
        (".svg", "image/svg+xml"),
    ):
        mimetypes.add_type(ctype, ext)


_register_web_mimetypes()

_WEB_UI_DIST = Path(__file__).parent / "static" / "web-ui"
_WEB_UI_HTML_CACHE_CONTROL = "no-cache"
_WEB_UI_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
_WEB_UI_STATIC_CACHE_CONTROL = "public, max-age=3600"
_WEB_UI_API_FALLBACK_PREFIXES = frozenset({"api", "auth", "health", "v1"})
_WEB_UI_GZIP_MINIMUM_SIZE = 1024
_CLAUDE_NATIVE_AGENT_NAME = CLAUDE_NATIVE_CODING_AGENT.agent_name
_CODEX_NATIVE_AGENT_NAME = CODEX_NATIVE_CODING_AGENT.agent_name
_PI_NATIVE_AGENT_NAME = PI_NATIVE_CODING_AGENT.agent_name
_OPENCODE_NATIVE_AGENT_NAME = OPENCODE_NATIVE_CODING_AGENT.agent_name
_CURSOR_NATIVE_AGENT_NAME = CURSOR_NATIVE_CODING_AGENT.agent_name
_KIRO_NATIVE_AGENT_NAME = KIRO_NATIVE_CODING_AGENT.agent_name
_ANTIGRAVITY_NATIVE_AGENT_NAME = ANTIGRAVITY_NATIVE_CODING_AGENT.agent_name
_QWEN_NATIVE_AGENT_NAME = QWEN_NATIVE_CODING_AGENT.agent_name
_KIMI_NATIVE_AGENT_NAME = KIMI_NATIVE_CODING_AGENT.agent_name
_DEBBY_AGENT_NAME = "debby"
_POLLY_AGENT_NAME = "polly"
_UNMATCHED_ROUTE_TEMPLATE = "<unmatched>"
# polly's and debby's multi-file bundles are packaged under
# omnigent.resources.examples (see pyproject package-data), so they resolve
# in both a repo checkout and an installed wheel. The presence check in each
# seeder is a safety net.
# resolve_repo_symlink dereferences the packaged symlink on a no-symlink
# Windows checkout (where Git leaves it as a stub text file); a no-op elsewhere.
_DEBBY_BUNDLE_SOURCE = resolve_repo_symlink(Path(_examples_resources.__file__).parent / "debby")
_POLLY_BUNDLE_SOURCE = resolve_repo_symlink(Path(_examples_resources.__file__).parent / "polly")


class _FastAPICallNext(Protocol):
    """
    Protocol for FastAPI's middleware continuation callable.
    """

    def __call__(self, request: Request) -> Awaitable[Response]:
        """
        Execute the next middleware or route handler.

        :param request: Incoming FastAPI request.
        :returns: Awaitable that resolves to the downstream response.
        """
        ...


class _WebSocketMetricsMiddleware:
    """
    ASGI middleware that tracks accepted WebSocket connections.

    :param app: Downstream ASGI app.
    :param metrics: Process-local server metrics tracker.
    """

    def __init__(self, app: ASGIApp, metrics: ServerPerformanceMetrics) -> None:
        """
        Initialize the middleware.

        :param app: Downstream ASGI app.
        :param metrics: Process-local server metrics tracker.
        """
        self._app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Track an accepted WebSocket for the lifetime of its ASGI scope.

        :param scope: ASGI connection scope, e.g. type ``"websocket"``.
        :param receive: ASGI receive callable.
        :param send: ASGI send callable.
        """
        if scope["type"] != "websocket":
            await self._app(scope, receive, send)
            return

        counted = False

        async def send_with_metrics(message: Message) -> None:
            """
            Count the connection when the route accepts the handshake.

            :param message: ASGI message emitted by the downstream app,
                e.g. ``{"type": "websocket.accept"}``.
            """
            nonlocal counted
            if not counted and message["type"] == "websocket.accept":
                self._metrics.websocket_connected()
                counted = True
            await send(message)

        try:
            await self._app(scope, receive, send_with_metrics)
        finally:
            if counted:
                self._metrics.websocket_disconnected()


def request_route_template_for_metrics(request: Request) -> str:
    """
    Return the low-cardinality route template for metrics attributes.

    Prefer the matched Starlette/FastAPI route template over the raw
    URL path so request IDs embedded in paths do not become metric
    label values.

    :param request: Incoming FastAPI request, e.g. ``GET /health``.
    :returns: Route template such as ``"/v1/sessions/{session_id}"``,
        or ``"<unmatched>"`` if no matched route is available.
    """
    route = request.scope.get("route")
    if isinstance(route, Route | Mount):
        return route.path
    return _UNMATCHED_ROUTE_TEMPLATE


def _request_status_code_for_metrics(
    status_code: int | None,
    *,
    failed: bool,
) -> int | None:
    """
    Return the HTTP status code to attach to request duration metrics.

    :param status_code: Response status code observed from
        ``call_next``.
    :param failed: Whether the request raised before returning a
        response.
    :returns: The observed status code, ``500`` for an exception
        before response creation, or ``None`` if unavailable.
    """
    if status_code is not None:
        return status_code
    if failed:
        return 500
    return None


# MCP startup warming moved to runner; see designs/RUNNER_MCP.md.


def _normalize_tarinfo(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    """
    Strip nondeterministic metadata from a tar member header.

    The built-in bundle builders tar a materialized directory whose
    files carry install-time metadata — a fresh wheel install on each
    deploy stamps new mtimes (and may differ in mode bits) even when
    the spec content is identical. Zeroing mtime/ownership and pinning
    a canonical mode makes the tarball a pure function of file paths +
    content, so the bundle's SHA-256 only moves when the spec actually
    changes. That is what lets the seeder skip no-op refreshes across
    redeploys instead of bumping the version every boot. Mode is safe
    to canonicalize because extraction uses ``set_attrs=False`` (see
    ``omnigent.spec.tar_utils.extract_safe``), so on-disk permissions
    don't depend on it.

    :param tarinfo: The tar member header to normalize.
    :returns: The same header with mtime, ownership, and mode pinned.
    """
    tarinfo.mtime = 0
    tarinfo.uid = 0
    tarinfo.gid = 0
    tarinfo.uname = ""
    tarinfo.gname = ""
    tarinfo.mode = 0o755 if tarinfo.isdir() else 0o644
    return tarinfo


def _tar_gz_dir(bundle_dir: Path) -> bytes:
    """
    Pack *bundle_dir* into a deterministic gzipped tarball.

    Identical directory content always yields identical bytes (gzip
    mtime pinned to 0, tar member metadata normalized via
    :func:`_normalize_tarinfo`), so the result is safe to
    content-address.

    :param bundle_dir: Materialized bundle directory to archive,
        e.g. a temp dir containing ``config.yaml``.
    :returns: Reproducible gzipped tarball bytes suitable for the
        artifact store.
    """
    import gzip
    import io

    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tf,
    ):
        # tarfile.add recurses in sorted(os.listdir) order, so member order
        # is stable across machines; _normalize_tarinfo zeroes the metadata.
        tf.add(str(bundle_dir), arcname=".", filter=_normalize_tarinfo)
    return buf.getvalue()


def _ensure_builtin_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
    *,
    name: str,
    bundle_bytes: bytes,
) -> None:
    """
    Register or refresh a built-in template agent from its bundle.

    Content-aware and idempotent. The agent row is keyed by *name*;
    its ``bundle_location`` is content-addressed
    (``"{agent_id}/{sha256}"``):

    - **No existing row** → create it.
    - **Row exists, content hash differs** → store the new bundle and
      update the row in place (keeps the ``agent_id`` stable so task
      history isn't cascade-deleted; bumps ``version`` so the runner's
      version-keyed spec cache re-fetches), then warm-swap the cache.
    - **Row exists, content hash matches** → evict the local cache so
      the next load re-fetches from ``bundle_location``, then return.

    The evict on the matching-hash path matters because
    :meth:`AgentCache.load` is keyed by ``agent_id`` and trusts its
    in-memory / on-disk entry without checking ``bundle_location``: a
    replica that boots with a cache lagging the (already-current) DB
    row — or a prior boot whose ``replace`` failed after ``update``
    succeeded — would otherwise keep serving the stale spec.

    This replaces the old seed-once behavior, which skipped on row
    existence and so served a stale spec after the wheel shipped a new
    one. Mirrors the upsert in :func:`omnigent.cli._register_yaml_bundle`.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs; exposes
        ``replace`` and ``evict``.
    :param name: Built-in agent's unique name, e.g. ``"polly"``.
    :param bundle_bytes: Freshly built gzipped tarball of the spec.
    """
    import hashlib

    from omnigent.db.utils import builtin_agent_id

    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    existing = agent_store.get_by_name(name)
    if existing is not None:
        new_loc = f"{existing.id}/{bundle_hash}"
        if existing.bundle_location == new_loc:
            # Row current; evict so a lagging replica's stale cache reloads the bundle.
            agent_cache.evict(existing.id)
            return
        artifact_store.put(new_loc, bundle_bytes)
        agent_store.update(existing.id, new_loc)
        # Warm-swap, not evict: a bare evict leaves the stale on-disk cache tier.
        # Built-ins are operator-authored template agents, so ${VAR} may expand
        # against the server env.
        agent_cache.replace(existing.id, new_loc, bundle_bytes, expand_env=True)
        _logger.info(
            "Refreshed built-in %s agent %s to bundle %s",
            name,
            existing.id,
            bundle_hash[:12],
        )
        return

    # Name-derived (not random) so it survives a per-pod reseed; see builtin_agent_id.
    agent_id = builtin_agent_id(name)
    bundle_key = f"{agent_id}/{bundle_hash}"
    artifact_store.put(bundle_key, bundle_bytes)
    agent_store.create(agent_id, name, bundle_key)
    agent_cache.evict(agent_id)
    _logger.info("Registered built-in %s agent as %s", name, agent_id)


def _ensure_default_agents(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """
    Register all built-in agents that should always be available.

    Called on every server lifespan startup. Each helper is
    content-aware via :func:`_ensure_builtin_agent`: it creates the
    agent if missing and refreshes it in place when the packaged
    bundle changed, so a redeploy picks up a new spec instead of
    serving the row seeded on first boot.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs.
    """
    _ensure_default_claude_agent(agent_store, artifact_store, agent_cache)
    _ensure_default_codex_agent(agent_store, artifact_store, agent_cache)
    _ensure_default_pi_agent(agent_store, artifact_store, agent_cache)
    _ensure_default_opencode_agent(agent_store, artifact_store, agent_cache)
    _ensure_default_cursor_agent(agent_store, artifact_store, agent_cache)
    _ensure_default_kiro_agent(agent_store, artifact_store, agent_cache)
    _ensure_default_antigravity_agent(agent_store, artifact_store, agent_cache)
    _ensure_default_qwen_agent(agent_store, artifact_store, agent_cache)
    _ensure_default_kimi_native_agent(agent_store, artifact_store, agent_cache)
    _ensure_default_debby_agent(agent_store, artifact_store, agent_cache)
    _ensure_default_polly_agent(agent_store, artifact_store, agent_cache)
    _ensure_extra_builtin_agents(agent_store, artifact_store, agent_cache)


# Env var listing extra built-in agent specs to seed at startup, in addition
# to the packaged claude-native-ui / codex-native-ui / polly set. Each
# ``os.pathsep``-separated entry is a path to an agent spec (single-file
# YAML or a bundle dir); it is registered as a built-in (``session_id NULL``)
# under the spec path's stem (file) or directory name. Lets a deployment —
# or an e2e fixture — ship custom always-available agents (e.g. a plain
# claude-sdk chat agent that a fork can switch into).
_EXTRA_BUILTIN_AGENTS_ENV = "OMNIGENT_BUILTIN_AGENT_DIRS"


def _ensure_extra_builtin_agents(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """
    Seed extra built-in agents named by :data:`_EXTRA_BUILTIN_AGENTS_ENV`.

    No-op when the env var is unset. Each entry is materialized into a
    bundle, tarballed, and registered via :func:`_ensure_builtin_agent`
    (content-aware / idempotent), so a redeploy refreshes a changed spec.
    The built-in's name is the entry path's stem (single-file spec) or
    directory name (bundle dir).

    Unlike the packaged ``_ensure_default_*`` helpers, this reads
    operator-supplied paths that may be wrong in a deployment (typo, stale
    mount). A bad entry is logged and skipped — one misconfigured extra
    agent must never block server startup (the packaged built-ins still
    seed). Mirrors the best-effort spec-load in :func:`_to_agent_object`.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs.
    """
    import tempfile

    from omnigent.spec import materialize_bundle

    raw = os.environ.get(_EXTRA_BUILTIN_AGENTS_ENV, "").strip()
    if not raw:
        return
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        source = Path(entry)
        try:
            name = source.stem if source.is_file() else source.name
            with tempfile.TemporaryDirectory() as tmpdir:
                bundle_dir = materialize_bundle(source, Path(tmpdir) / "bundle")
                bundle_bytes = _tar_gz_dir(bundle_dir)
            _ensure_builtin_agent(
                agent_store,
                artifact_store,
                agent_cache,
                name=name,
                bundle_bytes=bundle_bytes,
            )
        except Exception:  # a bad operator path must not block server startup
            _logger.exception(
                "Failed to register extra built-in agent from %r (%s); skipping. Check %s.",
                str(source),
                "does not exist" if not source.exists() else "invalid spec/bundle",
                _EXTRA_BUILTIN_AGENTS_ENV,
            )
            continue
        _logger.info("Registered extra built-in agent %r from %s", name, source)


def _build_claude_native_bundle() -> bytes:
    """
    Build a gzipped tarball of the claude-native-ui agent spec.

    :returns: Gzipped tarball bytes suitable for the artifact store.
    """
    import tempfile

    from omnigent.claude_native import _materialize_claude_agent_spec
    from omnigent.spec import materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = _materialize_claude_agent_spec(Path(tmpdir))
        bundle_dir = materialize_bundle(spec_path, Path(tmpdir) / "bundle")
        return _tar_gz_dir(bundle_dir)


def _ensure_default_claude_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """
    Register or refresh the claude-native-ui agent.

    Called during server lifespan startup so the Web UI can create
    host-launched sessions without requiring a prior CLI-initiated
    session. Content-aware via :func:`_ensure_builtin_agent`: a new
    wheel with a changed spec refreshes the row in place rather than
    being ignored.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs.
    """
    _ensure_builtin_agent(
        agent_store,
        artifact_store,
        agent_cache,
        name=_CLAUDE_NATIVE_AGENT_NAME,
        bundle_bytes=_build_claude_native_bundle(),
    )


def _build_codex_native_bundle() -> bytes:
    """
    Build a gzipped tarball of the codex-native-ui agent spec.

    :returns: Gzipped tarball bytes suitable for the artifact store.
    """
    import tempfile

    from omnigent.codex_native import _materialize_codex_agent_spec
    from omnigent.spec import materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = _materialize_codex_agent_spec(Path(tmpdir), model=None)
        bundle_dir = materialize_bundle(spec_path, Path(tmpdir) / "bundle")
        return _tar_gz_dir(bundle_dir)


def _ensure_default_codex_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """
    Register or refresh the codex-native-ui agent.

    Called during server lifespan startup so the Web UI can offer
    Codex as a built-in agent alongside Claude. Content-aware via
    :func:`_ensure_builtin_agent`: a new wheel with a changed spec
    refreshes the row in place rather than being ignored.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs.
    """
    _ensure_builtin_agent(
        agent_store,
        artifact_store,
        agent_cache,
        name=_CODEX_NATIVE_AGENT_NAME,
        bundle_bytes=_build_codex_native_bundle(),
    )


def _build_opencode_native_bundle() -> bytes:
    """
    Build a gzipped tarball of the opencode-native-ui agent spec.

    :returns: Gzipped tarball bytes suitable for the artifact store.
    """
    import tempfile

    from omnigent.opencode_native import _materialize_opencode_agent_spec
    from omnigent.spec import materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = _materialize_opencode_agent_spec(Path(tmpdir), model=None)
        bundle_dir = materialize_bundle(spec_path, Path(tmpdir) / "bundle")
        return _tar_gz_dir(bundle_dir)


def _ensure_default_opencode_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """
    Register or refresh the opencode-native-ui agent.

    Called during server lifespan startup so the Web UI can offer OpenCode
    as a built-in agent alongside Claude / Codex / Pi. Content-aware via
    :func:`_ensure_builtin_agent`: a new wheel with a changed spec refreshes
    the row in place rather than being ignored.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs.
    """
    _ensure_builtin_agent(
        agent_store,
        artifact_store,
        agent_cache,
        name=_OPENCODE_NATIVE_AGENT_NAME,
        bundle_bytes=_build_opencode_native_bundle(),
    )


def _build_pi_native_bundle() -> bytes:
    """
    Build a gzipped tarball of the pi-native-ui agent spec.

    :returns: Gzipped tarball bytes suitable for the artifact store.
    """
    import tempfile

    from omnigent.pi_native import _materialize_pi_agent_spec
    from omnigent.spec import materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = _materialize_pi_agent_spec(Path(tmpdir))
        bundle_dir = materialize_bundle(spec_path, Path(tmpdir) / "bundle")
        return _tar_gz_dir(bundle_dir)


def _ensure_default_pi_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """
    Register or refresh the pi-native-ui agent.

    Called during server lifespan startup so the Web UI can offer Pi as a
    built-in native-terminal agent. Content-aware via
    :func:`_ensure_builtin_agent`: a new wheel with a changed spec refreshes
    the row in place rather than being ignored.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs.
    """
    _ensure_builtin_agent(
        agent_store,
        artifact_store,
        agent_cache,
        name=_PI_NATIVE_AGENT_NAME,
        bundle_bytes=_build_pi_native_bundle(),
    )


def _build_cursor_native_bundle() -> bytes:
    """
    Build a gzipped tarball of the cursor-native-ui agent spec.

    :returns: Gzipped tarball bytes suitable for the artifact store.
    """
    import tempfile

    from omnigent.cursor_native import _materialize_cursor_agent_spec
    from omnigent.spec import materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = _materialize_cursor_agent_spec(Path(tmpdir))
        bundle_dir = materialize_bundle(spec_path, Path(tmpdir) / "bundle")
        return _tar_gz_dir(bundle_dir)


def _ensure_default_cursor_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """
    Register or refresh the cursor-native-ui agent.

    Called during server lifespan startup so the Web UI offers Cursor as a
    built-in native-terminal agent on every deployment (not only after the
    ``omnigent cursor`` CLI first registers it). Content-aware via
    :func:`_ensure_builtin_agent`.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs.
    """
    _ensure_builtin_agent(
        agent_store,
        artifact_store,
        agent_cache,
        name=_CURSOR_NATIVE_AGENT_NAME,
        bundle_bytes=_build_cursor_native_bundle(),
    )


def _build_kiro_native_bundle() -> bytes:
    """Build a gzipped tarball of the kiro-native-ui agent spec."""
    import tempfile

    from omnigent.kiro_native import _materialize_kiro_agent_spec
    from omnigent.spec import materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = _materialize_kiro_agent_spec(Path(tmpdir), model=None)
        bundle_dir = materialize_bundle(spec_path, Path(tmpdir) / "bundle")
        return _tar_gz_dir(bundle_dir)


def _ensure_default_kiro_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """Register or refresh the kiro-native-ui agent."""
    _ensure_builtin_agent(
        agent_store,
        artifact_store,
        agent_cache,
        name=_KIRO_NATIVE_AGENT_NAME,
        bundle_bytes=_build_kiro_native_bundle(),
    )


def _ensure_default_antigravity_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """
    Register or refresh the antigravity-native-ui agent.

    Called during server lifespan startup so the Web UI can offer Antigravity
    as a built-in native-terminal agent (the ``agy`` TUI), alongside Claude
    Code / Codex / Pi. Content-aware via :func:`_ensure_builtin_agent`: a new
    wheel with a changed spec refreshes the row in place rather than being
    ignored.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs.
    """
    _ensure_builtin_agent(
        agent_store,
        artifact_store,
        agent_cache,
        name=_ANTIGRAVITY_NATIVE_AGENT_NAME,
        bundle_bytes=_build_antigravity_native_bundle(),
    )


def _build_antigravity_native_bundle() -> bytes:
    """
    Build a gzipped tarball of the antigravity-native-ui agent spec.

    :returns: Gzipped tarball bytes suitable for the artifact store.
    """
    import tempfile

    from omnigent.antigravity_native import _materialize_antigravity_agent_spec
    from omnigent.spec import materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = _materialize_antigravity_agent_spec(Path(tmpdir))
        bundle_dir = materialize_bundle(spec_path, Path(tmpdir) / "bundle")
        return _tar_gz_dir(bundle_dir)


def _build_qwen_native_bundle() -> bytes:
    """
    Build a gzipped tarball of the qwen-native-ui agent spec.

    :returns: Gzipped tarball bytes suitable for the artifact store.
    """
    import tempfile

    from omnigent.qwen_native import _materialize_qwen_agent_spec
    from omnigent.spec import materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = _materialize_qwen_agent_spec(Path(tmpdir))
        bundle_dir = materialize_bundle(spec_path, Path(tmpdir) / "bundle")
        return _tar_gz_dir(bundle_dir)


def _ensure_default_qwen_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """
    Register or refresh the qwen-native-ui agent.

    Called during server lifespan startup so the Web UI offers Qwen Code as a
    built-in native-terminal agent on every deployment (not only after the
    ``omnigent qwen`` CLI first registers it). Content-aware via
    :func:`_ensure_builtin_agent`.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs.
    """
    _ensure_builtin_agent(
        agent_store,
        artifact_store,
        agent_cache,
        name=_QWEN_NATIVE_AGENT_NAME,
        bundle_bytes=_build_qwen_native_bundle(),
    )


def _build_kimi_native_bundle() -> bytes:
    """
    Build a gzipped tarball of the kimi-native-ui agent spec.

    :returns: Gzipped tarball bytes suitable for the artifact store.
    """
    import tempfile

    from omnigent.kimi_native import _materialize_kimi_agent_spec
    from omnigent.spec import materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = _materialize_kimi_agent_spec(Path(tmpdir))
        bundle_dir = materialize_bundle(spec_path, Path(tmpdir) / "bundle")
        return _tar_gz_dir(bundle_dir)


def _ensure_default_kimi_native_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """
    Register or refresh the kimi-native-ui agent.

    Called during server lifespan startup so the Web UI offers Kimi as a
    built-in native-terminal agent on every deployment (not only after the
    ``omnigent kimi`` CLI first registers it). Content-aware via
    :func:`_ensure_builtin_agent`.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs.
    """
    _ensure_builtin_agent(
        agent_store,
        artifact_store,
        agent_cache,
        name=_KIMI_NATIVE_AGENT_NAME,
        bundle_bytes=_build_kimi_native_bundle(),
    )


def _build_debby_bundle() -> bytes:
    """
    Build a gzipped tarball of the ``examples/debby`` agent bundle.

    debby is a multi-file image (``config.yaml`` plus ``agents/`` and
    ``skills/`` subdirectories), so the source is the directory itself
    rather than a generated single YAML.

    :returns: Gzipped tarball bytes suitable for the artifact store.
    """
    import tempfile

    from omnigent.spec import materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = materialize_bundle(_DEBBY_BUNDLE_SOURCE, Path(tmpdir) / "bundle")
        return _tar_gz_dir(bundle_dir)


def _ensure_default_debby_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """
    Register the debby brainstorming agent if its bundle ships here.

    Called during server lifespan startup so the Web UI's new-session
    picker can offer debby as a host-launchable card next to Claude
    Code, Codex, and polly. When the bundle is absent (generic
    deployment that didn't package it), seeding is skipped so no card
    is offered for an agent that can't be launched here — same pattern
    as :func:`_ensure_default_polly_agent`. Content-aware via
    :func:`_ensure_builtin_agent`: when a new wheel ships a changed
    debby spec, the existing row is refreshed in place instead of
    being ignored.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs.
    """
    if not (_DEBBY_BUNDLE_SOURCE / "config.yaml").is_file():
        _logger.debug(
            "debby bundle not found at %s; skipping seed",
            _DEBBY_BUNDLE_SOURCE,
        )
        return

    _ensure_builtin_agent(
        agent_store,
        artifact_store,
        agent_cache,
        name=_DEBBY_AGENT_NAME,
        bundle_bytes=_build_debby_bundle(),
    )


def _build_polly_bundle() -> bytes:
    """
    Build a gzipped tarball of the ``examples/polly`` agent bundle.

    polly is a multi-file image (``config.yaml`` plus ``agents/`` and
    ``skills/`` subdirectories), so the source is the directory itself
    rather than a generated single YAML.

    :returns: Gzipped tarball bytes suitable for the artifact store.
    """
    import tempfile

    from omnigent.spec import materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = materialize_bundle(_POLLY_BUNDLE_SOURCE, Path(tmpdir) / "bundle")
        return _tar_gz_dir(bundle_dir)


def _ensure_default_polly_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: Any,
) -> None:
    """
    Register the polly orchestrator agent if its bundle ships here.

    polly is the multi-agent coding orchestrator (successor to the
    deleted nessie example); seeding it lets the Web UI's new-session
    picker offer it as a host-launchable card next to Claude Code and
    Codex. When the bundle is absent (generic deployment that didn't
    package it), seeding is skipped so no card is offered for an agent
    that can't be launched here — mirroring the ``_WEB_UI_DIST``
    "asset present → enable feature" pattern. Content-aware via
    :func:`_ensure_builtin_agent`: when a new wheel ships a changed
    polly spec, the existing row is refreshed in place instead of
    being ignored.

    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for agent bundles.
    :param agent_cache: Cache for loaded agent specs.
    """
    if not (_POLLY_BUNDLE_SOURCE / "config.yaml").is_file():
        _logger.debug(
            "polly bundle not found at %s; skipping seed",
            _POLLY_BUNDLE_SOURCE,
        )
        return

    _ensure_builtin_agent(
        agent_store,
        artifact_store,
        agent_cache,
        name=_POLLY_AGENT_NAME,
        bundle_bytes=_build_polly_bundle(),
    )


def create_app(
    agent_store: AgentStore,
    file_store: FileStore,
    conversation_store: ConversationStore,
    artifact_store: ArtifactStore,
    agent_cache: AgentCache,
    runner_tunnel_tokens: frozenset[str] | None = None,
    comment_store: CommentStore | None = None,
    policy_store: PolicyStore | None = None,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
    host_store: HostStore | None = None,
    account_store: Any | None = None,  # SqlAlchemyAccountStore — accounts mode only
    extra_routers: list[tuple[Any, str, list[str]]] | None = None,
    policy_modules: list[str] | None = None,
    admins: list[str] | None = None,
    allowed_domains: list[str] | None = None,
    sandbox_config: ManagedSandboxConfig | None = None,
) -> FastAPI:
    """
    Build and return the FastAPI application with all routes mounted.

    Stores and cache are injected here and passed to route factories.
    Each dependency is forwarded to the router factories that need it;
    the app itself only wires them together.

    :param agent_store: Store for agent CRUD operations.
    :param file_store: Store for uploaded-file metadata.
    :param conversation_store: Store for conversation and
        conversation-item persistence.
    :param artifact_store: Store for binary blobs (agent bundles,
        file content).
    :param agent_cache: Cache for loaded agent specs and working
        directories.
    :param runner_tunnel_tokens: Optional allow-list of binding
        tokens accepted by the runner WebSocket tunnel route, e.g.
        ``frozenset({"uA6Zz..."})``. ``None`` accepts any
        token-bound runner id, which is the shared remote-server
        behavior.
    :param comment_store: Store for per-conversation review comments.
    :param policy_store: Store for server-persisted policies
        (session-scoped and server-wide defaults). ``None``
        disables both the session policy and default policy
        CRUD endpoints.
    :param permission_store: Store for session-level access grants.
        ``None`` disables permission checks (all access allowed).
    :param auth_provider: Pre-constructed auth provider for
        identity resolution. ``None`` disables auth (anonymous
        access). **Required** when ``permission_store`` is
        provided — raises ``ValueError`` otherwise. Callers
        construct the provider via ``create_auth_provider()``
        or a custom implementation.
    :param host_store: Store for host registrations. ``None``
        disables host connectivity features (list hosts, launch
        runners on remote hosts).
    :param policy_modules: Additional dotted module paths to
        scan for ``POLICY_REGISTRY`` lists at startup, e.g.
        ``["myorg.policies.safety"]``. Sourced from the server
        config's ``policy_modules`` key. ``None`` scans only
        the built-in modules.
    :param admins: Admin identities from the server config's
        ``admins:`` key, e.g. ``["alice@example.com"]``. Union'd with
        the runtime-editable ``<data_dir>/admins`` file; a matching
        identity is promoted to admin on login.
    :param allowed_domains: Allowed email domains from the server
        config's ``allowed_domains:`` key (OIDC), e.g. ``["example.com"]``.
        Union'd with ``OMNIGENT_OIDC_ALLOWED_DOMAINS`` and the
        runtime-editable domains file.
    :param sandbox_config: Parsed ``sandbox:`` section of the server
        config — which provider to provision managed hosts
        (``host_type="managed"`` sessions) from and the URL they dial
        back to. ``None`` disables managed hosts (a
        ``host_type="managed"`` create fails with a clear error).
        Managed-host credentials live on the ``hosts`` table, so no
        extra store is wired.
    :returns: A fully configured :class:`FastAPI` application.
    :raises ValueError: If ``permission_store`` is provided
        without an ``auth_provider``.
    """
    if permission_store is not None and auth_provider is None:
        raise ValueError("auth_provider is required when permission_store is provided")

    # First-boot admin bootstrap for the accounts auth provider.
    # Runs before any route is mounted so the login page is never
    # served against an empty user table (avoids the Immich-style
    # land-grab race — see designs/oss-cuj/01-research-summary.md
    # §2.2.1). Guarded on (a) accounts source active, (b)
    # auth_provider wired in, and (c) account_store passed in.
    #
    # account_store is an EXPLICIT parameter (not constructed in
    # here) so the internal hosted product can opt out of accounts
    # persistence entirely by passing None — even when it happens
    # to deploy with the accounts code on disk. Without this gate
    # the create_app factory would force every consumer to carry
    # an AccountStore, defeating the whole "accounts is opt-in"
    # framing.
    _bootstrap_result = None  # populated below for the lifespan hook
    if auth_provider is not None and account_store is not None:
        from omnigent.server.auth import UnifiedAuthProvider

        if isinstance(auth_provider, UnifiedAuthProvider) and auth_provider._source == "accounts":
            from omnigent.server.accounts_bootstrap import bootstrap_admin

            _accounts_cfg = auth_provider._accounts_config
            _bootstrap_result = bootstrap_admin(
                account_store,
                init_admin_password=_accounts_cfg.init_admin_password,
                base_url=_accounts_cfg.base_url,
                session_ttl_hours=_accounts_cfg.session_ttl_hours,
                cookie_secret=_accounts_cfg.cookie_secret,
            )

    from omnigent.runner.routing import RunnerRouter
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
    from omnigent.server.host_registry import HostRegistry, RunnerExitReports

    tunnel_registry = TunnelRegistry()
    runner_router = RunnerRouter(
        registry=tunnel_registry,
        conversation_store=conversation_store,
    )
    host_registry = HostRegistry()
    # Shared between the host tunnel (which records ``host.runner_exited``
    # reports from daemons) and the runner status endpoint (which surfaces
    # them to clients waiting for a launched runner to connect).
    runner_exit_reports = RunnerExitReports()
    # AP-server-side MCP proxy pool. Manages connections to agents'
    # external MCP servers on behalf of the
    # ``POST /v1/sessions/{id}/mcp`` endpoint. Created here (before the
    # lifespan and before the sessions router is registered) so the same
    # object is closed by the lifespan and held by the router closure.
    # ``ServerMcpPool.__init__`` is synchronous and safe to call outside
    # a running event loop.
    _mcp_pool = ServerMcpPool()
    server_metrics = ServerPerformanceMetrics()
    server_metrics_otel = ServerMetricsOtelPublisher()

    @asynccontextmanager
    async def _lifespan(
        app_inst: FastAPI,
    ) -> AsyncIterator[None]:
        """FastAPI lifespan: start/stop the harness process manager
        and tear down the tmux terminal registry on shutdown.

        On startup: construct + start the
        :class:`HarnessProcessManager` and stash it on
        ``app.state.harness_process_manager`` for workflow
        dispatch to use when routing through the harness contract
        (see ``designs/SERVER_HARNESS_CONTRACT.md`` §Process
        management).

        On shutdown: shut down the harness process manager (which
        terminates every per-conversation runner subprocess and
        cleans up the per-AP-instance dir) and close every live
        tmux terminal in the :class:`TerminalRegistry`. Terminal
        cleanup is best-effort with per-instance timeouts; see
        ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §4.4.

        :param app_inst: The FastAPI app, used to attach
            per-AP state via ``app_inst.state.*``.
        """
        # Bump AnyIO default thread limiter from 40 → 200; every
        # ``asyncio.to_thread`` and FastAPI sync route grabs one.
        from anyio import to_thread as _to_thread

        _to_thread.current_default_thread_limiter().total_tokens = 200

        # Apply OMNIGENT_LOG_LEVEL to the omnigent namespace after
        # uvicorn's dictConfig runs (dictConfig resets existing handlers,
        # making a pre-run basicConfig call ineffective).
        import os as _os

        _log_level_name = _os.environ.get("OMNIGENT_LOG_LEVEL", "INFO").upper()
        logging.getLogger("omnigent").setLevel(getattr(logging, _log_level_name, logging.INFO))

        harness_pm = HarnessProcessManager()
        await harness_pm.start()
        # Store on both ``app.state`` (canonical, accessible from
        # routes) AND a runtime-module global (workflows access it
        # via ``get_harness_process_manager()`` because workflows
        # can't easily receive non-serializable args).
        app_inst.state.harness_process_manager = harness_pm
        set_harness_process_manager(harness_pm)

        set_runner_router(runner_router)

        # Wake a blocked sub-agent's immediate parent: hooks
        # ``pending_elicitations.record_publish`` to post a ``[System: …]``
        # notice to the parent's ``/events``. Uninstalled at teardown so a
        # fresh app instance doesn't inherit a prior run's observer (matters
        # for multi-app test setups).
        from omnigent.server.routes.sessions import (
            configure_subagent_block_notifier,
        )

        _uninstall_subagent_block_notifier = configure_subagent_block_notifier(
            conversation_store,
            runner_router,
        )

        from omnigent.runner.resource_registry import (
            SessionResourceRegistry,
        )
        from omnigent.runtime import set_resource_registry

        resource_reg = SessionResourceRegistry(
            terminal_registry=get_terminal_registry(),
        )
        set_resource_registry(resource_reg)

        # Install the tunnel-backed WS factory so browser terminal
        # attach can proxy frames over the same persistent WebSocket
        # the runner already uses for HTTP.
        from omnigent.server._runner_ws_tunnel import make_tunnel_ws_factory

        set_runner_ws_factory(make_tunnel_ws_factory(runner_router, tunnel_registry))

        # MCP execution moved to the runner (designs/RUNNER_MCP.md);
        # SessionFilesystemRegistry moved to the runner. Both
        # warmup blocks deleted here.

        _ensure_default_agents(agent_store, artifact_store, agent_cache)

        # Populate the policy registry (builtins + user-configured
        # modules) so GET /v1/policy-registry serves the catalog.
        from omnigent.policies.registry import load_registry

        load_registry(extra_modules=policy_modules)

        # Accounts first-run: open the browser after uvicorn has bound
        # the port. bootstrap_admin sets open_url to the loopback base
        # URL on a needs-setup boot so the browser lands on the
        # Create-admin form. Gated on (a) bootstrap asked for an open,
        # and (b) the auto-open env var is truthy (default ON; CLI
        # passes OMNIGENT_ACCOUNTS_AUTO_OPEN=0 for --no-open). Broad
        # try so a missing display / browser never blocks startup.
        if _bootstrap_result is not None and _bootstrap_result.open_url:
            from omnigent.server.auth import env_var_is_truthy

            if env_var_is_truthy("OMNIGENT_ACCOUNTS_AUTO_OPEN", default=True):
                import webbrowser

                try:
                    webbrowser.open(_bootstrap_result.open_url)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "accounts: auto-open browser failed (%s) — open the "
                        "server URL in a browser instead",
                        exc,
                    )

        metrics_publish_task = asyncio.create_task(
            publish_server_metrics_periodically(
                server_metrics,
                otel_publisher=server_metrics_otel,
            )
        )
        try:
            yield
        finally:
            metrics_publish_task.cancel()
            with suppress(asyncio.CancelledError):
                await metrics_publish_task
            # Stop in-flight background managed-sandbox launches so a
            # slow provision doesn't outlive the ASGI shutdown (the
            # sandbox itself, if already provisioned, is reaped by the
            # provider lifetime cap — see the hook's docstring).
            from omnigent.server.routes.sessions import cancel_managed_launch_tasks

            await cancel_managed_launch_tasks()
            _uninstall_subagent_block_notifier()
            set_resource_registry(None)
            set_runner_ws_factory(None)
            set_runner_router(None)
            await runner_router.aclose()

            set_harness_process_manager(None)
            await harness_pm.shutdown()
            await get_terminal_registry().shutdown()
            # Shut down all AP-side MCP connections opened by the proxy
            # endpoint. Best-effort — individual close failures are logged
            # inside shutdown_all().
            await _mcp_pool.shutdown_all()

    app = FastAPI(title="Omnigent Server", lifespan=_lifespan)
    from omnigent.runtime import telemetry

    telemetry.instrument_fastapi_app(app)
    # Expose the registry on app.state so integration tests and
    # diagnostics can verify that the production app wires the route
    # and WSTunnelTransport to the same session registry.
    app.state.tunnel_registry = tunnel_registry
    app.state.runner_router = runner_router
    app.state.host_registry = host_registry
    app.state.host_store = host_store
    app.state.sandbox_config = sandbox_config
    # Tracks in-flight background managed-host launches (POST
    # /v1/sessions returns before the sandbox exists) so a message
    # racing the provision can rendezvous instead of failing with
    # "no runner bound". Always wired — cheap, and post_event probes
    # it regardless of whether managed hosts are configured.
    from omnigent.server.managed_hosts import ManagedLaunchTracker

    app.state.managed_launches = ManagedLaunchTracker()
    app.state.server_metrics = server_metrics
    app.state.server_metrics_otel = server_metrics_otel
    app.add_middleware(_WebSocketMetricsMiddleware, metrics=server_metrics)
    # CSWSH guard: reject cross-origin WebSocket handshakes before any
    # route accepts them. Added after the metrics middleware so it is the
    # outermost WS middleware — a forbidden origin is closed without even
    # reaching the metrics counter (which only counts on accept anyway).
    app.add_middleware(WebSocketOriginMiddleware)
    # Give the tool-policy ASK gate (which forwards the native-terminal
    # approval popup from a parked-gate background task, off any
    # request/route closure) the runner router so it can reach the bound
    # runner.
    set_server_runner_router(runner_router)

    @app.middleware("http")
    async def _record_server_metrics(
        request: Request,
        call_next: _FastAPICallNext,
    ) -> Response:
        """
        Count each HTTP request while it is being processed.

        :param request: Incoming FastAPI request, e.g. ``GET /health``.
        :param call_next: FastAPI middleware continuation that executes
            the matched route and returns its response.
        :returns: The downstream route response.
        """
        failed = False
        status_code: int | None = None
        started_at = server_metrics.request_started()
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            failed = True
            raise
        finally:
            request_failed = failed or (status_code is not None and status_code >= 500)
            duration_seconds = server_metrics.request_finished(
                started_at=started_at,
                failed=request_failed,
            )
            set_request_duration_for_access_log(duration_seconds)
            route = request_route_template_for_metrics(request)
            metrics_status_code = _request_status_code_for_metrics(
                status_code,
                failed=failed,
            )
            server_metrics_otel.record_request_duration(
                duration_seconds=duration_seconds,
                failed=request_failed,
                method=request.method,
                route=route,
                status_code=metrics_status_code,
            )

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        """
        Convert application errors to structured JSON responses.

        :param request: The incoming request (unused — FastAPI signature requirement).
        :param exc: The application error.
        :returns: A JSON response with the error code and message.
        """
        if exc.http_status >= 500:
            _logger.error("Internal error: %s", exc.message, exc_info=True)
        elif exc.http_status == 400 and request.url.path.endswith("/policies/evaluate"):
            _logger.warning(
                "Policy evaluate rejected 400 on %s: %s", request.url.path, exc.message
            )
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(Exception)
    async def _handle_unhandled_exception(
        request: Request,  # noqa: ARG001 — FastAPI exception-handler signature requires (request, exc); we only use exc
        exc: Exception,
    ) -> JSONResponse:
        """
        Catch-all for unhandled exceptions (e.g. database
        OperationalError). Returns the standard JSON error schema
        so clients always get a consistent response format.

        :param request: The incoming request (unused — FastAPI signature requirement).
        :param exc: The unhandled exception.
        :returns: A 500 JSON response with ``internal_error`` code.
        """
        _logger.error("Unhandled exception: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": ErrorCode.INTERNAL_ERROR,
                    "message": "An internal error occurred.",
                },
            },
        )

    def _host_is_online(host_id: str) -> bool:
        """
        Return whether ``host_id`` is currently online, cross-replica.

        Reads from the ``hosts`` DB table (the cross-replica source of
        truth, written by the tunnel endpoint on connect/disconnect)
        rather than the per-replica :class:`HostRegistry`. If the host
        is connected to replica B and the request lands on replica A,
        A's registry won't know about it — the DB will. Mirrors the
        same change made in ``routes/hosts.py`` for ``GET /v1/hosts``.

        When ``host_store`` was not supplied (host support not wired),
        fall back to the local registry: in that configuration no row
        is ever written so the DB would return ``None``.

        A persisted ``"online"`` status is not trusted on its own: a
        host that crashed without a graceful disconnect never runs
        ``set_offline`` and stays ``"online"`` forever. ``is_online``
        also requires the host's last-seen to be fresh
        (``HOST_LIVENESS_TTL_S``), so a stale row reads as offline.

        :param host_id: Host identifier from the session row.
        :returns: ``True`` when the host is online and was seen
            recently.
        """
        if host_store is None:
            return host_registry.get(host_id) is not None
        return host_store.is_online(host_id)

    def _bulk_hosts_online(host_ids: list[str]) -> set[str]:
        """
        Return the subset of ``host_ids`` that are currently online.

        Bulk variant of :func:`_host_is_online` for the sidebar
        online-dot batch path: one ``SELECT ... IN (...)`` over the
        hosts table instead of a per-host query. Same cross-replica
        DB-backed liveness gate; falls back to the per-replica
        registry when ``host_store`` is not wired.

        :param host_ids: Host identifiers to check, e.g.
            ``["host_abc123", "host_def456"]``. Empty input returns
            an empty set.
        :returns: The set of ids whose host is online and fresh.
        """
        if not host_ids:
            return set()
        if host_store is None:
            return {h for h in host_ids if host_registry.get(h) is not None}
        return host_store.online_host_ids(host_ids)

    def _bulk_host_versions(host_ids: list[str]) -> dict[str, str]:
        """
        Map each requested host_id to the version from its live hello frame.

        Resolved from the in-memory host registry only: the host version
        isn't persisted to the hosts table, so a host connected to another
        replica (multi-replica ``host_store`` deploys) is absent here and
        the caller reports ``host_version=None`` for that session — the info
        popover then simply omits the host version. Single-server /
        single-replica deploys (the common case) resolve it fully. The
        registry lookup is an in-memory dict read, so this stays off the
        DB hot path the surrounding bulk liveness query optimizes.

        :param host_ids: Bound host identifiers to resolve, e.g.
            ``["host_abc123"]``. Empty input returns an empty map.
        :returns: ``{host_id: version}`` for every id with a live local
            tunnel; ids without one are absent.
        """
        versions: dict[str, str] = {}
        for host_id in set(host_ids):
            host_conn = host_registry.get(host_id)
            if host_conn is not None:
                versions[host_id] = host_conn.hello.version
        return versions

    def _session_liveness(sid: str) -> SessionLiveness:
        """
        Resolve strict runner + host liveness for one session.

        Single-id wrapper around :func:`_bulk_session_liveness`. See
        that function for the full liveness semantics.

        :param sid: Session/conversation id, e.g. ``"conv_abc123"``.
        :returns: The :class:`SessionLiveness` pair for ``sid``. An id
            with no conversation row resolves to
            ``runner_online=True`` (no runner ⇒ reachable) and
            ``host_online=None`` (no host binding).
        """
        return _bulk_session_liveness([sid]).get(
            sid, SessionLiveness(runner_online=True, host_online=None)
        )

    def _bulk_session_liveness(ids: list[str]) -> dict[str, SessionLiveness]:
        """
        Bulk strict-liveness check with a fixed, small number of SQL
        queries.

        Resolves every session's connectivity in two batch reads —
        one over the conversations table (runner/host binding, via
        :meth:`ConversationStore.get_session_connectivity`) and one
        over the hosts table (:meth:`HostStore.online_host_ids`) —
        rather than the per-session ``get_conversation`` + per-host
        ``is_online`` fan-out the sidebar poll used to drive. That
        fan-out was ``O(n)`` synchronous Lakebase round-trips per
        ``/health`` call (~3 per offline session); for the sidebar's
        20-session batch it serialized ~60 queries on the request,
        and because ``/health`` ran them on the event loop it blocked
        every other in-flight request. See :func:`_session_liveness`
        for the single-id wrapper.

        ``runner_online`` is **strict**: ``True`` iff a runner tunnel
        is currently registered for the session
        (:func:`_runner_up`). It deliberately does **not** fold in
        host-relaunch optimism — a dead runner on a live host reads
        ``runner_online=False`` here, paired with ``host_online=True``
        so the open-session view can offer "send a message to wake
        the runner" without misreporting reachability. ``host_online``
        is populated from the same online-hosts batch: ``True`` when
        the session's ``host_id`` is online and fresh, ``False`` when
        a ``host_id`` is set but offline/stale, and ``None`` when the
        session has no ``host_id`` (CLI / local). Liveness is purely
        "is the tunnel up / is the host fresh" — there is no longer a
        deliberate-stop marker that forces a session offline (Stop is
        non-sticky: it drops the runner tunnel, which is reflected here
        as ``runner_online=False``, and the next message relaunches on
        a live host).

        :param ids: Session/conversation ids to check, e.g.
            ``["conv_abc123", "conv_def456"]``.
        :returns: Mapping ``session_id -> SessionLiveness``. Ids with
            no conversation row default to
            ``SessionLiveness(runner_online=True, host_online=None)``
            (mirrors the legacy single-session path, which treated a
            missing row as reachable).
        """
        connectivity = conversation_store.get_session_connectivity(ids)

        def _runner_up(conn: SessionConnectivity) -> bool:
            """A bound runner whose tunnel is currently registered."""
            return conn.runner_id is not None and tunnel_registry.get(conn.runner_id) is not None

        # Resolve host liveness for every bound host in one query, so
        # ``host_online`` can be reported even when the runner tunnel is
        # up (the open-session view shows host state regardless).
        host_ids_to_check = {
            conn.host_id for conn in connectivity.values() if conn.host_id is not None
        }
        online_hosts = _bulk_hosts_online(list(host_ids_to_check))
        host_versions = _bulk_host_versions(list(host_ids_to_check))
        result: dict[str, SessionLiveness] = {}
        for sid in ids:
            conn = connectivity.get(sid)
            if conn is None:
                # No conversation row — treat as reachable with no host
                # binding, matching the legacy single-session behavior.
                result[sid] = SessionLiveness(runner_online=True, host_online=None)
                continue
            if conn.host_id is None:
                host_online: bool | None = None
                host_version: str | None = None
            else:
                host_online = conn.host_id in online_hosts
                host_version = host_versions.get(conn.host_id)
            if conn.runner_id is None:
                # No runner binding: an in-process executor (or a session
                # not yet dispatched) is reachable — EXCEPT an unbound fork
                # of a session that had a working directory, which must
                # rebind a host + directory first. Reporting it offline
                # routes the first message into the directory picker instead
                # of dropping it against a runner that can't start.
                runner_online = not conn.needs_workspace
            else:
                # Strict: reachable only if the runner tunnel is up. No
                # host-relaunch optimism — host state lives in host_online.
                runner_online = _runner_up(conn)
            result[sid] = SessionLiveness(
                runner_online=runner_online,
                host_online=host_online,
                host_version=host_version,
            )
        return result

    @app.get("/health")
    async def health(
        session_id: str | None = Query(default=None),
        session_ids: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """
        Liveness check with optional session-scoped runner status.

        Without session params, returns ``{"status": "ok"}`` (bare
        liveness). With ``session_id``, adds a single ``session``
        object. With ``session_ids`` (comma-separated), adds a
        ``sessions`` dict keyed by id — used by the sidebar to
        batch-check all visible sessions in one request. The batch
        path runs a single SQL ``IN`` query, not N per-id round-trips.

        Each per-session object carries both ``runner_online`` (strict
        runner reachability) and ``host_online`` (host tunnel live, or
        ``None`` when the session has no host binding) — see
        :class:`~omnigent.server.routes.sessions.SessionLiveness`.

        :param session_id: Optional single session id, e.g.
            ``"conv_abc123"``.
        :param session_ids: Optional comma-separated session ids
            for batch lookup, e.g.
            ``"conv_abc,conv_def,conv_ghi"``.
        :returns: ``{"status": "ok"}`` with optional ``session``
            and/or ``sessions`` fields. Each session object has shape
            ``{"runner_online": bool, "host_online": bool | None,
            "host_version": str | None}`` (the single ``session``
            object also includes its ``id``). ``host_version`` is the
            bound host's reported version, or ``None`` when there's no
            host binding / the version isn't resolvable on this replica.
        """
        result: dict[str, Any] = {"status": "ok"}
        batch_ids = [s.strip() for s in session_ids.split(",") if s.strip()] if session_ids else []
        # Resolve every requested id (single + batch) in ONE lookup. The
        # online-dot lookups hit the database (conversations + hosts
        # tables) and MUST run in a worker thread, not inline on the
        # event loop: the sidebar polls this endpoint every 1-2s with
        # every visible session id, and a single-worker uvicorn deploy
        # shares one event loop across all requests. Running the blocking
        # psycopg calls inline serialized every concurrent request behind
        # them (a trivial conversation load queued for seconds). See
        # ``_bulk_session_liveness`` for the query-count reduction.
        all_ids = ([session_id] if session_id is not None else []) + batch_ids
        liveness = await asyncio.to_thread(_bulk_session_liveness, all_ids) if all_ids else {}
        # Missing ids default to reachable / no-host, matching the bulk
        # lookup's own missing-row terminal.
        _missing = SessionLiveness(runner_online=True, host_online=None)
        if session_id is not None:
            single = liveness.get(session_id, _missing)
            result["session"] = {
                "id": session_id,
                "runner_online": single.runner_online,
                "host_online": single.host_online,
                "host_version": single.host_version,
            }
        if session_ids is not None:
            result["sessions"] = {
                sid: {
                    "runner_online": (sl := liveness.get(sid, _missing)).runner_online,
                    "host_online": sl.host_online,
                    "host_version": sl.host_version,
                }
                for sid in batch_ids
            }
        return result

    @app.get("/api/version")
    async def version() -> dict[str, str]:
        """
        Return the installed omnigent package version.

        Used by the web UI to include version info in bug reports.

        :returns: ``{"version": "<semver string>"}``,
            e.g. ``{"version": "0.1.0"}``.
        """
        from importlib.metadata import version as _pkg_version

        return {"version": _pkg_version("omnigent")}

    @app.get("/v1/info")
    async def info() -> dict[str, bool | str | None]:
        """Runtime capabilities probe for the SPA + CLI.

        Returned at app boot by the frontend (and by ``omnigent
        login`` when it needs to choose between flows). Drives
        conditional route registration and chrome on the SPA side
        — when ``accounts_enabled`` is false, the SPA never
        registers ``/login``, ``/register``, ``/members`` and
        never renders the AccountMenu, so the bundle behaves
        identically to a pre-PR-2008 build for header / OIDC
        deploys (in particular, the internal hosted product that
        syncs from this repo).

        Authentication: this endpoint is intentionally UNAUTHED
        so the SPA can probe it before holding a session cookie.
        It exposes no sensitive state — only the active auth
        source, the login URL, whether first-run admin setup is
        still pending (``needs_setup``), coarse capability
        booleans (``databricks_features``,
        ``managed_sandboxes_enabled``), the short sandbox
        provider name (``sandbox_provider``) the web UI labels the
        new-session sandbox option with, and the installed
        ``server_version`` (already public via ``/api/version``).
        """
        from omnigent.server.auth import UnifiedAuthProvider

        accounts_enabled = (
            isinstance(auth_provider, UnifiedAuthProvider) and auth_provider._source == "accounts"
        )
        login_url = getattr(auth_provider, "login_url", None)
        # needs_setup drives the SPA's first-run "Create admin" form:
        # true only in accounts mode while no password-having account
        # exists yet. Same predicate bootstrap_admin uses, computed
        # live so it flips to false the instant /auth/setup (or any
        # login) creates the first admin. Exposing it is safe — it's a
        # boolean about whether setup is pending, not a secret.
        needs_setup = False
        if accounts_enabled and account_store is not None:
            needs_setup = not any(u.has_password for u in account_store.list_users())
        # databricks_features gates the Databricks-deployment-only UI hints
        # (the "Databricks Lakebox" connect tab). True only when the internal
        # lakebox launcher module is present — it is excluded from the OSS
        # export, so an OSS build reports False and the SPA shows the clean,
        # provider-agnostic hints. find_spec is side-effect-free (no import).
        import importlib.util

        databricks_features = (
            importlib.util.find_spec("omnigent.onboarding.sandboxes.lakebox") is not None
        )
        # managed_sandboxes_enabled gates the web UI's sandbox
        # option on the new-session screen: true only when a `sandbox:`
        # config is wired AND its provider can actually serve a managed
        # launch (staged providers parse but reject at launch — they
        # must not advertise the option).
        managed_sandboxes_enabled = (
            sandbox_config is not None and sandbox_config.managed_launch_supported
        )
        # sandbox_provider names the backing provider (e.g. "modal",
        # "islo") so the web UI can label the option per provider
        # ("Modal Sandbox" / "Islo Sandbox") instead of the
        # generic "New Sandbox". Only surfaced when the option is
        # actually offered; None when no provider is named (embedding
        # configs may leave it unset) so the UI keeps the generic label.
        sandbox_provider = sandbox_config.provider if managed_sandboxes_enabled else None
        # server_version is the installed omnigent package version (same
        # source as /api/version), surfaced so the web UI can show it in the
        # session info popover alongside the per-session host version.
        from importlib.metadata import version as _pkg_version

        # smart_routing_enabled: true when the server can route — either
        # a RoutingClient is explicitly configured (OMNIGENT_SMART_ROUTING=1
        # + llm: config) or the managed deployment registered a
        # policy_llm_connection_factory (which means it has LLM capability
        # and will supply its own RoutingClient).
        try:
            from omnigent.runtime._globals import _caps

            smart_routing_enabled = _caps is not None and (
                _caps.routing_client is not None or _caps.policy_llm_connection_factory is not None
            )
        except ImportError:
            smart_routing_enabled = False
        return {
            "accounts_enabled": accounts_enabled,
            "login_url": login_url,
            "needs_setup": needs_setup,
            "databricks_features": databricks_features,
            "managed_sandboxes_enabled": managed_sandboxes_enabled,
            "sandbox_provider": sandbox_provider,
            "server_version": _pkg_version("omnigent"),
            "smart_routing_enabled": smart_routing_enabled,
        }

    @app.get("/v1/me", response_model=None)  # Union return type (dict | JSONResponse)
    async def me(request: Request) -> dict[str, str | None] | JSONResponse:
        """Return the current user's identity.

        Reads the user from the auth provider (same logic that
        session routes use). The frontend calls this on load to
        discover who it is.

        When OIDC is active and the user is unauthenticated,
        returns 401 with a ``login_url`` so the frontend knows
        where to redirect.

        :param request: The incoming FastAPI request.
        :returns: ``{"user_id": "alice@example.com"}``,
            ``{"user_id": null}`` if unauthenticated in header
            mode, or 401 with ``login_url`` in OIDC mode.
        """
        user_id: str | None = None
        if auth_provider is not None:
            user_id = auth_provider.get_user_id(request)
        login_url = getattr(auth_provider, "login_url", None)
        if user_id is None and login_url is not None:
            return JSONResponse(
                status_code=401,
                content={"user_id": None, "login_url": login_url},
            )
        return {"user_id": user_id}

    app.include_router(
        create_sessions_router(
            conversation_store,
            agent_store,
            file_store=file_store,
            artifact_store=artifact_store,
            runner_router=runner_router,
            auth_provider=auth_provider,
            permission_store=permission_store,
            agent_cache=agent_cache,
            mcp_pool=_mcp_pool,
            # Lets WS /v1/sessions/updates fold runner + host liveness into
            # its pushes so the web app can drop its GET /health poll.
            liveness_lookup=_bulk_session_liveness,
            # Lets GET /sessions and WS /sessions/updates carry the
            # per-session comments fingerprint so the web app refreshes
            # its comment list on external mutations.
            comment_store=comment_store,
            # Same allow-list the tunnel router gets: authorizes runner
            # writes to the policy-owned cost_control.* session labels.
            runner_tunnel_tokens=runner_tunnel_tokens,
            # Lets the session snapshot surface a crashed runner's cause
            # (host.runner_exited) as last_task_error so a reload still
            # renders the error banner after the live push is gone.
            runner_exit_reports=runner_exit_reports,
        ),
        prefix="/v1",
        tags=["sessions"],
    )
    # Read-only built-in agent discovery (designs/BUILTIN_AGENTS.md).
    # Successor to the removed GET /api/agents list; lists only
    # built-in (session_id IS NULL) agents for the new-session picker.
    app.include_router(
        create_builtin_agents_router(
            agent_store,
            agent_cache,
            artifact_store=artifact_store,
            auth_provider=auth_provider,
        ),
        prefix="/v1",
        tags=["agents"],
    )
    app.include_router(
        create_terminal_attach_router(
            auth_provider=auth_provider,
            permission_store=permission_store,
            conversation_store=conversation_store,
        ),
        prefix="/v1",
        tags=["terminals"],
    )
    app.include_router(
        create_session_mcp_servers_router(
            conversation_store,
            agent_store,
            artifact_store,
            agent_cache,
            runner_router=runner_router,
            auth_provider=auth_provider,
            permission_store=permission_store,
        ),
        prefix="/v1",
        tags=["session_mcp_servers"],
    )
    if comment_store is not None:
        app.include_router(
            create_comments_router(
                comment_store,
                auth_provider=auth_provider,
                permission_store=permission_store,
                conversation_store=conversation_store,
            ),
            prefix="/v1",
            tags=["comments"],
        )
    if policy_store is not None:
        app.include_router(
            create_session_policies_router(
                policy_store,
                conversation_store,
                auth_provider=auth_provider,
                permission_store=permission_store,
            ),
            prefix="/v1",
            tags=["session_policies"],
        )
        app.include_router(
            create_default_policies_router(
                policy_store,
                auth_provider=auth_provider,
                permission_store=permission_store,
            ),
            prefix="/v1",
            tags=["default_policies"],
        )
    app.include_router(
        create_policy_registry_router(auth_provider=auth_provider),
        prefix="/v1",
        tags=["policy_registry"],
    )

    # ── Tunnel lifecycle callbacks (Step 8.5 crash recovery) ───
    async def _on_runner_disconnect(runner_id: str) -> None:
        """Mark sessions pinned to *this* runner as offline.

        Filters by ``runner_id`` against ``conversation_store`` so a
        disconnect on one runner does not flip every cached session
        (e.g. sessions owned by other runners on the same server, or
        sessions left in the module-level cache by earlier tests on
        the same xdist worker) to ``"failed"``. The cache is updated
        in lockstep with the publish so the list endpoint stays
        coherent.

        :param runner_id: The disconnected runner's id.
        """
        from omnigent.server.routes.sessions import (
            _publish_status,
            _session_status_cache,
        )

        # Direct by-runner lookup: read-after-write consistent (the
        # listing path may be served from an eventually-consistent
        # search index in alternate store backends) and
        # O(sessions-on-this-runner) instead of a 500-row scan.
        # Archived sessions are included by construction — an archived
        # session can still be runner-bound, and skipping it here would
        # leave it stuck "running" forever.
        affected = [
            c.id
            for c in await asyncio.to_thread(
                conversation_store.list_conversations_by_runner_id, runner_id
            )
        ]
        _logger.warning(
            "Runner %s disconnected; marking %d session(s) offline",
            runner_id,
            len(affected),
        )
        for session_id in affected:
            _session_status_cache[session_id] = "failed"
            _publish_status(session_id, "failed")

    async def _on_runner_exited(runner_id: str, error: str) -> None:
        """Mark a crashed runner's session(s) failed and push the cause.

        Fired by the host tunnel when a daemon reports
        ``host.runner_exited`` — the only failure signal for a runner
        that died before connecting its tunnel (so ``_on_runner_disconnect``
        never fires for it). Mirrors that callback's by-runner lookup,
        but carries the daemon-composed error onto the ``session.status:
        failed`` event so the open view surfaces the cause immediately
        instead of spinning on "starting" until a timeout.

        :param runner_id: The crashed runner's id.
        :param error: Human-readable cause from the daemon (exit code +
            log tail), e.g. ``"runner process exited with code 1 ..."``.
        """
        from omnigent.server.routes.sessions import (
            _publish_status,
            _session_status_cache,
        )
        from omnigent.server.schemas import ErrorDetail

        affected = [
            c.id
            for c in await asyncio.to_thread(
                conversation_store.list_conversations_by_runner_id, runner_id
            )
        ]
        _logger.warning(
            "Runner %s reported crashed; marking %d session(s) failed: %s",
            runner_id,
            len(affected),
            error,
        )
        detail = ErrorDetail(code="runner_failed_to_start", message=error)
        for session_id in affected:
            _session_status_cache[session_id] = "failed"
            _publish_status(session_id, "failed", error=detail)

    async def _on_runner_connect(runner_id: str) -> None:
        """Re-assign sessions and restart SSE relays on reconnect.

        Resolves the runner client per-session via
        ``runner_router.client_for_session_resources``. The legacy
        ``get_runner_client()`` returns ``None`` in multi-runner
        deployments where only ``set_runner_router`` is wired, so
        routing must go through the router.

        :param runner_id: The reconnecting runner's id.
        """
        from omnigent.server.routes.sessions import _ensure_runner_relay

        # Direct by-runner lookup instead of list-everything-and-filter:
        # the listing path may be backed by an eventually-consistent
        # search index in alternate store backends, which cannot see a
        # session created seconds ago — exactly the window this callback
        # runs in for a host-spawned runner. Missing the session here
        # means create_session never reaches the runner and the
        # claude-native terminal is never bootstrapped. Archived
        # sessions are included by construction (their relays must
        # restart on reconnect like any other).
        convs = await asyncio.to_thread(
            conversation_store.list_conversations_by_runner_id, runner_id
        )
        _logger.info(
            "_on_runner_connect: runner=%s, %d bound session(s)",
            runner_id,
            len(convs),
        )
        for conv in convs:
            _logger.info(
                "_on_runner_connect: matched %s (agent=%s)",
                conv.id,
                conv.agent_id,
            )
            try:
                routed = runner_router.client_for_session_resources(conv.id)
            except OmnigentError:
                _logger.exception(
                    "Failed to resolve runner client for session %s on reconnect",
                    conv.id,
                )
                continue
            if not conv.agent_id:
                # The runner's create_session requires agent_id (it 400s
                # without one), so don't send a request it rejects by
                # contract. The old list path filtered these rows out via
                # has_agent_id=True; the by-runner lookup returns them, and
                # the relay restart below still applies — the session is
                # runner-bound regardless of having an agent.
                _logger.debug(
                    "_on_runner_connect: skipping session-init POST for %s (no agent_id)",
                    conv.id,
                )
            else:
                try:
                    await routed.client.post(
                        "/v1/sessions",
                        json={
                            "session_id": conv.id,
                            "agent_id": conv.agent_id,
                        },
                        timeout=10.0,
                    )
                except Exception:
                    _logger.exception(
                        "Failed to re-assign session %s on reconnect",
                        conv.id,
                    )
            _ensure_runner_relay(
                conv.id,
                runner_id,
                routed.client,
                conversation_store,
            )

    # WS tunnel endpoint for runners (RUNNER.md §2-3).
    app.include_router(
        create_runner_tunnel_router(
            tunnel_registry,
            allowed_tunnel_tokens=runner_tunnel_tokens,
            on_runner_disconnect=_on_runner_disconnect,
            on_runner_connect=_on_runner_connect,
            auth_provider=auth_provider,
            runner_exit_reports=runner_exit_reports,
        ),
        prefix="/v1",
        tags=["runners"],
    )

    # Host tunnel + REST endpoints (DAEMON_API.md). Mounted only when a
    # host_store is configured: the routers call host_store on every
    # request, so mounting them with host_store=None would fail each
    # connection with an AttributeError swallowed by the tunnel's broad
    # except (a hidden failure). No host_store = host support is simply
    # not enabled (host connects get 404), rather than silently broken.
    if host_store is not None:
        from omnigent.server.routes.host_tunnel import create_host_tunnel_router
        from omnigent.server.routes.hosts import create_hosts_router

        app.include_router(
            create_host_tunnel_router(
                host_registry,
                host_store,
                auth_provider=auth_provider,
                runner_exit_reports=runner_exit_reports,
                on_runner_exited=_on_runner_exited,
            ),
            prefix="/v1",
            tags=["hosts"],
        )
        app.include_router(
            create_hosts_router(
                host_registry,
                host_store,
                conversation_store,
                auth_provider=auth_provider,
                permission_store=permission_store,
                agent_store=agent_store,
                agent_cache=agent_cache,
            ),
            prefix="/v1",
            tags=["hosts"],
        )

    # Mount the auth router that matches the active provider. OIDC and
    # accounts share the /auth prefix but expose different endpoints
    # under it (OIDC: /login, /callback, /logout, /cli-login, /cli-poll;
    # accounts: /login POST, /logout, /me, /invite, /register, /magic,
    # /magic/redeem, /users, /users/{id}/reset, /users/me/password).
    # Must be registered BEFORE the SPA static mount because the SPA's
    # HTML5-history fallback catches all unmatched extensionless paths.
    if auth_provider is not None and getattr(auth_provider, "login_url", None):
        from omnigent.server.admin_list import load_admin_list
        from omnigent.server.auth import UnifiedAuthProvider

        # Admin roster: the config ``admins:`` list (canonical) union'd
        # with the runtime-editable ``<data_dir>/admins`` file. Consulted
        # on each login to promote listed identities — the only admin
        # path for OIDC, and an additive convenience for accounts. The
        # file portion lazily reloads on mtime change (no restart).
        admin_list = load_admin_list(extra=frozenset(admins or ()))

        if (
            isinstance(auth_provider, UnifiedAuthProvider)
            and auth_provider._source == "accounts"
            and account_store is not None
        ):
            from omnigent.server.routes.accounts_auth import (
                create_accounts_auth_router,
            )

            app.include_router(
                create_accounts_auth_router(
                    auth_provider, account_store, admin_list, permission_store
                ),
                prefix="/auth",
                tags=["auth"],
            )
        else:
            from omnigent.server.routes.auth import create_auth_router

            # OIDC invites are opt-in (OMNIGENT_OIDC_ALLOW_INVITES) and
            # need the token/invited-email store. Construct one on the
            # shared DB when enabled and the caller didn't pass one —
            # OIDC deploys don't otherwise wire an account store.
            oidc_account_store = account_store
            _oidc_cfg = getattr(auth_provider, "_oidc_config", None)
            if (
                oidc_account_store is None
                and _oidc_cfg is not None
                and getattr(_oidc_cfg, "allow_invites", False)
                and permission_store is not None
            ):
                from omnigent.server.accounts_store import SqlAlchemyAccountStore

                oidc_account_store = SqlAlchemyAccountStore(permission_store.storage_location)

            app.include_router(
                create_auth_router(
                    auth_provider,
                    permission_store,
                    admin_list,
                    oidc_account_store,
                    allowed_domains=frozenset(allowed_domains or ()) or None,
                ),
                prefix="/auth",
                tags=["auth"],
            )

    # Mount the built ap-web SPA at "/" if a build is present. The SPA is
    # built into ``omnigent/server/static/web-ui/`` by ``ap-web/``'s Vite
    # build (see ``ap-web/vite.config.ts`` ``build.outDir``). The mount is
    # registered AFTER all API routers so router routes win on overlap.
    # Skipping the mount when no build is present keeps API-only
    # deployments working (and ``/`` 404s cleanly instead of exploding at
    # startup).
    #
    # `_SPAStaticFiles` adds an HTML5-history fallback: any path that
    # doesn't match a real file falls through to ``index.html``, which
    # lets client-side routes like ``/c/<conversation_id>`` survive a
    # browser refresh. Plain ``StaticFiles(html=True)`` only serves
    # ``index.html`` for the literal root and directory paths, so a
    # refresh on ``/c/abc`` would 404.
    # Extra routers injected by callers (e.g. test fixtures that
    # mount legacy routes). Registered BEFORE the SPA static-files
    # mount so FastAPI resolves them before the catch-all fallback.
    if extra_routers:
        for router, prefix, tags in extra_routers:
            app.include_router(router, prefix=prefix, tags=tags)

    web_ui_dist = _WEB_UI_DIST
    if web_ui_dist.is_dir() and (web_ui_dist / "index.html").is_file():
        app.mount(
            "/",
            _RangeAwareGZipMiddleware(
                _SPAStaticFiles(directory=web_ui_dist, html=True),
                minimum_size=_WEB_UI_GZIP_MINIMUM_SIZE,
            ),
            name="web-ui",
        )
    else:

        @app.get("/", include_in_schema=False)
        async def root() -> dict[str, str]:
            """
            Return API server metadata when no web UI build is bundled.

            Databricks Apps opens the app URL at ``/`` in a browser.
            API-only wheels intentionally omit the SPA assets, so serve
            a small JSON landing response instead of FastAPI's generic
            404. When a web UI build is present, the static mount above
            owns ``/`` and this fallback is not registered.

            :returns: Service metadata with health and docs paths.
            """
            return {
                "service": "omnigent",
                "status": "ok",
                "health": "/health",
                "docs": "/docs",
            }

    return app


class _SPAStaticFiles(StaticFiles):
    """``StaticFiles`` with an SPA history fallback.

    React Router's client-side routes (e.g. ``/c/abc123``) need to
    survive a browser refresh — landing on them directly should return
    the SPA shell, which then boots and resolves the route on the
    client. Plain ``StaticFiles(html=True)`` only serves ``index.html``
    for the literal root and directory paths, so a refresh on
    ``/c/abc`` would 404.

    The fallback is gated by an API-prefix and extension check: unmatched
    ``/v1`` / ``/api`` / ``/auth`` / ``/health`` paths return a JSON 404,
    and a path with a file extension (``.js``, ``.css``, ``.png``,
    ``.woff2``, …) returns the static 404 verbatim. Other extensionless
    paths fall back to ``index.html``.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # The mount is at "/" so it catches *every* unmatched path —
        # including WebSocket upgrades, which Starlette's StaticFiles
        # asserts against (raises ``AssertionError`` mid-handshake). A
        # WS request landing here means no router matched it (e.g. a
        # client targeting an endpoint that doesn't exist on this build
        # of the server). Reject cleanly with code 1011 instead of
        # crashing the ASGI worker.
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1011, "reason": "no such endpoint"})
            return
        await super().__call__(scope, receive, send)

    async def get_response(self, path: str, scope: Scope) -> Response:  # type: ignore[override]
        served_path = path
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and _is_web_ui_api_fallback_path(path):
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": {
                            "code": ErrorCode.NOT_FOUND,
                            "message": "Not found",
                        }
                    },
                )
            if exc.status_code == 404 and "." not in path.rsplit("/", 1)[-1]:
                served_path = "index.html"
                response = await super().get_response("index.html", scope)
            else:
                raise
        return _apply_web_ui_cache_headers(response, served_path)


def _is_web_ui_api_fallback_path(path: str) -> bool:
    """
    Return whether an unmatched static path belongs to the API namespace.

    The web UI is mounted at ``/`` and receives every unmatched request after
    the routers. Without this guard, an unknown API route such as
    ``/v1/sessions/x/codex_goal`` is served the SPA shell as ``200 text/html``,
    which makes browser clients fail with a JSON parse error instead of a
    route-level 404.

    :param path: Static mount-relative path, e.g. ``"v1/sessions/x"``.
    :returns: True for paths that should never fall back to ``index.html``.
    """
    first_segment = path.lstrip("/").split("/", 1)[0]
    return first_segment in _WEB_UI_API_FALLBACK_PREFIXES


class _RangeAwareGZipMiddleware(GZipMiddleware):
    """
    Gzip middleware that leaves ranged static-file responses unencoded.

    HTTP range metadata is defined over the selected representation. If
    Starlette's generic gzip middleware compresses a ``206`` response,
    the ``Content-Range`` header still describes the unencoded file
    while the body bytes are gzip-encoded. The web UI only needs gzip
    for normal full-file static asset fetches, so requests carrying a
    ``Range`` header bypass compression entirely.

    :param app: Static-file ASGI app to wrap.
    :param minimum_size: Minimum response body size to compress, e.g.
        ``1024``.
    :param compresslevel: gzip compression level to pass through to
        Starlette.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Compress ordinary static responses and pass range requests through.

        :param scope: ASGI request scope, e.g. type ``"http"``.
        :param receive: ASGI receive callable.
        :param send: ASGI send callable.
        :returns: None.
        """
        if scope["type"] == "http" and any(
            key.lower() == b"range" for key, _value in scope["headers"]
        ):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def _apply_web_ui_cache_headers(response: Response, path: str) -> Response:
    """
    Apply browser cache policy for the bundled web UI static mount.

    The SPA shell must be revalidated so a deployment can point clients
    at new chunk names, while Vite emits content-hashed files under
    ``assets/`` that are safe to cache immutably. Other static files
    get a modest cache lifetime because they may not be fingerprinted.

    :param response: Response produced by Starlette's static-file
        handler.
    :param path: Static mount-relative path that was served, e.g.
        ``"assets/index-AbCd1234.js"`` or ``"index.html"`` for an
        SPA history fallback.
    :returns: The same response with ``Cache-Control`` set.
    """
    content_type = response.headers.get("content-type")
    media_type = content_type.partition(";")[0].lower() if content_type is not None else None
    if path.startswith("assets/"):
        response.headers["Cache-Control"] = _WEB_UI_ASSET_CACHE_CONTROL
    elif media_type == "text/html" or path in {"", ".", "index.html"}:
        response.headers["Cache-Control"] = _WEB_UI_HTML_CACHE_CONTROL
    else:
        response.headers["Cache-Control"] = _WEB_UI_STATIC_CACHE_CONTROL
    return response
