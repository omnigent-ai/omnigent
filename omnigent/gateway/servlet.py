"""The gateway servlet: a loopback provider endpoint for gateway harnesses.

One asyncio listener per host, sized for hundreds of concurrent streaming
sessions (shared ``httpx.AsyncClient`` pool; chunked passthrough with no
buffering). Endpoints:

- ``GET  /g/{token}/v1/models`` — **implemented**: the live workspace model
  catalog in Codex's ``ModelsResponse`` shape, ETag/304 for the CLI's
  3-minute poll.
- ``*    /g/{token}/v1/{path}`` — transparent streaming passthrough to the
  session's workspace gateway, with a freshly minted Databricks bearer
  replacing the session's local token.
- ``/admin/*`` — loopback control plane (session registration, catalog for
  the host tunnel), guarded by the admin bearer published in the state file.

Fail-open posture throughout: a session that cannot register falls back to
the direct gateway URL at launch; a catalog that cannot build returns 503 so
Codex keeps its bundled list; upstream errors relay verbatim so the harness
sees the gateway's real status and body.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from omnigent.gateway.auth import TokenMinter, databrickscfg_host_for_profile
from omnigent.gateway.catalog import (
    build_models_response,
    catalog_etag,
    dumps_catalog,
    fetch_codex_service_ids,
    normalize_relay_model_body,
    picker_options,
    routable_models,
)
from omnigent.gateway.state import (
    DEFAULT_GATEWAY_PORT,
    ServletState,
    _pid_alive,
    clear_servlet_state,
    read_servlet_state,
    read_session_registry,
    write_servlet_state,
    write_session_registry,
)

_logger = logging.getLogger(__name__)

# End-to-end semantics must survive the relay; hop-by-hop headers must not.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)

_CATALOG_TTL_S = 300.0
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=120.0, pool=30.0)
_UPSTREAM_LIMITS = httpx.Limits(max_connections=1024, max_keepalive_connections=128)
_START_TIMEOUT_S = 10.0

# The Databricks codex surface, relative to a workspace origin.
_CODEX_GATEWAY_PATH = "/ai-gateway/codex/v1"


@dataclass(frozen=True)
class _Session:
    """One registered harness session.

    :param token: Unguessable path/bearer token identifying the session.
    :param profile: ``~/.databrickscfg`` profile minting its credentials.
    :param workspace_host: Workspace origin the session routes to.
    :param upstream_base: Upstream provider base, e.g.
        ``"https://x.databricks.com/ai-gateway/codex/v1"``.
    """

    token: str
    profile: str
    workspace_host: str
    upstream_base: str


class GatewayServlet:
    """Session registry + catalog cache + streaming passthrough."""

    def __init__(
        self,
        native_catalog_provider: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        """
        :param native_catalog_provider: Blocking callable returning Codex's
            own model catalog (``codex debug models`` output) used to enrich
            served entries; ``None`` disables ``/models`` (passthrough still
            works).
        """
        self.admin_token = secrets.token_urlsafe(24)
        self.stats: Counter[str] = Counter()
        self._sessions: dict[str, _Session] = {}
        self._minter = TokenMinter()
        self._native_provider = native_catalog_provider
        self._native_catalog: dict[str, Any] | None = None
        self._native_loaded = False
        self._native_lock = asyncio.Lock()
        # workspace_host -> (payload, etag, expires_at)
        self._catalog_cache: dict[str, tuple[bytes, str, float]] = {}
        self._catalog_locks: dict[str, asyncio.Lock] = {}
        self._client = httpx.AsyncClient(
            timeout=_UPSTREAM_TIMEOUT,
            limits=_UPSTREAM_LIMITS,
            follow_redirects=False,
        )
        # Restore sessions registered by a previous daemon: their base URLs
        # are frozen in live session configs, so the tokens must keep
        # resolving after a restart.
        for token, row in read_session_registry().items():
            self._sessions[token] = _Session(
                token=token,
                profile=row["profile"],
                workspace_host=row["workspace_host"],
                upstream_base=f"{row['workspace_host']}{_CODEX_GATEWAY_PATH}",
            )
        if self._sessions:
            _logger.info("gateway registry restored: %d session(s)", len(self._sessions))

    # ------------------------------------------------------------------ app

    def build_app(self) -> Starlette:
        """
        :returns: The ASGI app serving admin + per-session routes.
        """
        return Starlette(
            routes=[
                Route("/healthz", self._healthz, methods=["GET"]),
                Route("/admin/sessions", self._admin_register, methods=["POST"]),
                Route("/admin/catalog", self._admin_catalog, methods=["GET"]),
                Route("/g/{token}/v1/models", self._models, methods=["GET"]),
                Route(
                    "/g/{token}/v1/{path:path}",
                    self._proxy,
                    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
                ),
            ]
        )

    async def aclose(self) -> None:
        """Release the upstream connection pool."""
        await self._client.aclose()

    # ------------------------------------------------------- session registry

    def register_session(self, profile: str, workspace_host: str) -> _Session:
        """
        Register one harness session and mint its path token.

        :param profile: Databricks profile for credentials.
        :param workspace_host: Workspace origin (no trailing slash).
        :returns: The registered session.
        """
        session = _Session(
            token=secrets.token_urlsafe(18),
            profile=profile,
            workspace_host=workspace_host,
            upstream_base=f"{workspace_host}{_CODEX_GATEWAY_PATH}",
        )
        self._sessions[session.token] = session
        self._persist_registry()
        return session

    def _persist_registry(self) -> None:
        """Best-effort write-through of the session registry."""
        try:
            write_session_registry(
                {
                    token: {"profile": s.profile, "workspace_host": s.workspace_host}
                    for token, s in self._sessions.items()
                }
            )
        except OSError:
            _logger.warning("could not persist the gateway session registry", exc_info=True)

    # ------------------------------------------------------------- handlers

    async def _healthz(self, _request: Request) -> Response:
        return JSONResponse(
            {"status": "ok", "sessions": len(self._sessions), "stats": dict(self.stats)}
        )

    def _admin_authorized(self, request: Request) -> bool:
        supplied = request.headers.get("authorization") or ""
        return secrets.compare_digest(supplied, f"Bearer {self.admin_token}")

    def _workspace_host_for(self, profile: str, claimed: str | None) -> str | None:
        """
        Resolve *profile*'s workspace host, refusing mismatched claims.

        A minted bearer must only ever travel to the profile's own
        ``~/.databrickscfg`` host — honoring a caller-supplied origin would
        turn the servlet into a credential-exfiltration relay.

        :param profile: ``~/.databrickscfg`` profile name.
        :param claimed: Optional host the caller sent; must match the
            resolved host when present.
        :returns: The resolved host, or ``None`` when unresolvable or
            contradicted by *claimed*.
        """
        resolved = databrickscfg_host_for_profile(profile)
        if not isinstance(resolved, str) or not resolved.startswith("http"):
            return None
        resolved = resolved.rstrip("/")
        if claimed and claimed.rstrip("/") != resolved:
            return None
        return resolved

    async def _admin_register(self, request: Request) -> Response:
        if not self._admin_authorized(request):
            return JSONResponse({"error": "admin token required"}, status_code=401)
        try:
            body = json.loads(await request.body())
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        profile = body.get("profile")
        if not isinstance(profile, str) or not profile:
            return JSONResponse({"error": "profile required"}, status_code=400)
        # The payload is a pointer into shared host config: the profile name.
        # The host is always resolved here from ~/.databrickscfg; a claimed
        # workspace_host is only accepted when it matches the resolution.
        claimed = body.get("workspace_host")
        workspace_host = self._workspace_host_for(
            profile, claimed if isinstance(claimed, str) else None
        )
        if workspace_host is None:
            return JSONResponse(
                {
                    "error": (
                        f"no workspace host resolvable for profile {profile!r} "
                        "(or the claimed workspace_host does not match it)"
                    )
                },
                status_code=400,
            )
        session = self.register_session(profile, workspace_host)
        self.stats["sessions_registered"] += 1
        _logger.info(
            "gateway session registered (%s… -> %s, profile %r)",
            session.token[:6],
            session.workspace_host,
            profile,
        )
        return JSONResponse({"token": session.token})

    async def _admin_catalog(self, request: Request) -> Response:
        if not self._admin_authorized(request):
            return JSONResponse({"error": "admin token required"}, status_code=401)
        profile = request.query_params.get("profile") or ""
        if not profile:
            return JSONResponse({"error": "profile required"}, status_code=400)
        workspace_host = self._workspace_host_for(
            profile, request.query_params.get("workspace_host")
        )
        if workspace_host is None:
            return JSONResponse(
                {
                    "error": (
                        f"no workspace host resolvable for profile {profile!r} "
                        "(or the claimed workspace_host does not match it)"
                    )
                },
                status_code=400,
            )
        options = await self.catalog_options(profile=profile, workspace_host=workspace_host)
        if options is None:
            return JSONResponse({"error": "catalog unavailable"}, status_code=503)
        models, routable = options
        return JSONResponse({"models": models, "routable_models": routable})

    async def _models(self, request: Request) -> Response:
        session = self._sessions.get(request.path_params["token"])
        if session is None:
            return JSONResponse({"error": "unknown gateway session"}, status_code=404)
        built = await self._catalog_payload(session.profile, session.workspace_host)
        if built is None:
            # Fail open: Codex keeps its bundled catalog on 5xx.
            return JSONResponse({"error": "catalog unavailable"}, status_code=503)
        payload, etag = built
        self.stats["models_served"] += 1
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"etag": etag})
        return Response(payload, media_type="application/json", headers={"etag": etag})

    async def _proxy(self, request: Request) -> Response:
        session = self._sessions.get(request.path_params["token"])
        if session is None:
            return JSONResponse({"error": "unknown gateway session"}, status_code=404)
        path = request.path_params["path"]
        if ".." in path.split("/"):
            # httpx normalizes dot segments, so a crafted path could walk the
            # session token out of the /ai-gateway/codex/v1 surface and reach
            # arbitrary same-origin workspace APIs with the minted bearer.
            return JSONResponse({"error": "invalid path"}, status_code=404)
        try:
            bearer = await self._minter.bearer(session.profile)
        except RuntimeError as exc:
            _logger.warning("credential mint failed for %r: %s", session.profile, exc)
            return JSONResponse({"error": str(exc)}, status_code=502)
        url = f"{session.upstream_base}/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _HOP_BY_HOP
            and key.lower() not in ("authorization", "content-length")
        }
        headers["authorization"] = f"Bearer {bearer}"
        # Usage attribution: tag relayed traffic unless the client already
        # carries its own tags (starlette lowercases header names).
        headers.setdefault("databricks-ai-gateway-request-tags", '{"source": "omnigent"}')
        # Byte-faithful relay: never advertise encodings the harness didn't
        # ask for. Without this, httpx adds its own accept-encoding and the
        # upstream's compressed error bodies reach a client that can't
        # decode them (observed: codex rendering a gzip error as garbage).
        headers.setdefault("accept-encoding", "identity")
        body = normalize_relay_model_body(await request.body())
        upstream_request = self._client.build_request(
            request.method, url, content=body, headers=headers
        )
        try:
            upstream = await self._client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            _logger.warning("upstream error for /%s: %s", path, exc)
            return JSONResponse(
                {"error": f"gateway servlet upstream error: {exc}"}, status_code=502
            )
        self.stats["relayed_requests"] += 1
        if upstream.status_code in (401, 403):
            # The workspace rejected the minted bearer — it is dead no matter
            # what the cache clock says (the CLI can hand out a near-expiry
            # token). Drop it so the very next request re-mints instead of
            # failing for the rest of the cache window.
            self._minter.invalidate(session.profile)
            _logger.warning(
                "upstream auth rejection (%s) for profile %r; minted bearer invalidated",
                upstream.status_code,
                session.profile,
            )
        _logger.info(
            "relay %s /%s -> %s (%s, session %s…)",
            request.method,
            path,
            upstream.status_code,
            session.workspace_host,
            session.token[:6],
        )

        async def stream() -> Any:
            # Raw (undecoded) chunks so Content-Encoding stays end-to-end;
            # yielded as they arrive so SSE first-token latency survives.
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        response = StreamingResponse(stream(), status_code=upstream.status_code)
        # multi_items + raw_headers keep repeated headers (e.g. Set-Cookie)
        # intact — a dict comprehension would collapse them to the last value.
        response.raw_headers = [
            (key.encode("latin-1"), value.encode("latin-1"))
            for key, value in upstream.headers.multi_items()
            if key.lower() not in _HOP_BY_HOP and key.lower() != "content-length"
        ]
        return response

    # -------------------------------------------------------------- catalog

    async def catalog_options(
        self, *, profile: str, workspace_host: str
    ) -> tuple[list[dict[str, object]], list[str]] | None:
        """
        Picker rows + routable ids for a workspace (host-tunnel consumption).

        :param profile: Databricks profile for credentials.
        :param workspace_host: Workspace origin (no trailing slash).
        :returns: ``(models, routable_models)`` or ``None`` when the catalog
            is unavailable.
        """
        built = await self._catalog_payload(profile, workspace_host)
        if built is None:
            return None
        models_response = json.loads(built[0])
        return picker_options(models_response), routable_models(models_response)

    async def _native(self) -> dict[str, Any] | None:
        if self._native_loaded:
            return self._native_catalog
        async with self._native_lock:
            if self._native_loaded:
                return self._native_catalog
            catalog: dict[str, Any] | None = None
            if self._native_provider is not None:
                try:
                    catalog = await asyncio.to_thread(self._native_provider)
                except Exception:
                    _logger.exception("native codex catalog probe failed")
            self._native_catalog = catalog if isinstance(catalog, dict) else None
            self._native_loaded = True
            return self._native_catalog

    async def _catalog_payload(
        self, profile: str, workspace_host: str
    ) -> tuple[bytes, str] | None:
        now = time.monotonic()
        cached = self._catalog_cache.get(workspace_host)
        if cached is not None and cached[2] > now:
            return cached[0], cached[1]
        lock = self._catalog_locks.setdefault(workspace_host, asyncio.Lock())
        async with lock:
            cached = self._catalog_cache.get(workspace_host)
            if cached is not None and cached[2] > time.monotonic():
                return cached[0], cached[1]
            native = await self._native()
            if native is None:
                return None
            try:
                bearer = await self._minter.bearer(profile)
                service_ids = await fetch_codex_service_ids(self._client, workspace_host, bearer)
            except (RuntimeError, httpx.HTTPError):
                _logger.warning(
                    "model-services listing failed for %s", workspace_host, exc_info=True
                )
                return None
            models_response = build_models_response(service_ids, native)
            if models_response is None:
                return None
            payload = dumps_catalog(models_response)
            etag = catalog_etag(payload)
            self._catalog_cache[workspace_host] = (
                payload,
                etag,
                time.monotonic() + _CATALOG_TTL_S,
            )
            _logger.info("catalog built for %s: %d models", workspace_host, len(service_ids))
            return payload, etag


@dataclass
class GatewayHandle:
    """Running servlet + its lifecycle, held by the host daemon."""

    url: str
    servlet: GatewayServlet
    _server: uvicorn.Server
    _task: asyncio.Task[None]

    async def catalog_options(
        self, *, profile: str, workspace_host: str
    ) -> tuple[list[dict[str, object]], list[str]] | None:
        """Delegate to :meth:`GatewayServlet.catalog_options`."""
        return await self.servlet.catalog_options(profile=profile, workspace_host=workspace_host)

    async def stop(self) -> None:
        """Stop the listener, close the pool, and retract the state file."""
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        await self.servlet.aclose()
        clear_servlet_state(os.getpid())


def _port_of(url: str) -> int | None:
    """
    Extract the port from a published servlet URL.

    :param url: e.g. ``"http://127.0.0.1:6768"``.
    :returns: The port, or ``None`` when unparsable.
    """
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).port
    except ValueError:
        return None


async def _serve_on(app: Starlette, port: int) -> tuple[uvicorn.Server, asyncio.Task[None], int]:
    """
    Bind and start serving *app* on one loopback port.

    :param app: The servlet ASGI app.
    :param port: Port to bind; ``0`` lets the OS choose.
    :returns: ``(server, serve_task, bound_port)``.
    :raises RuntimeError: When the bind or startup fails.
    """
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    # Bind here rather than inside uvicorn: a busy port then surfaces as a
    # catchable RuntimeError instead of uvicorn's SystemExit killing the
    # daemon before the next candidate port is tried.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        sock.close()
        raise RuntimeError(f"gateway servlet could not bind port {port}: {exc}") from exc
    bound_port = sock.getsockname()[1]
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]), name="gateway-servlet")
    deadline = time.monotonic() + _START_TIMEOUT_S
    while not server.started:
        if task.done():
            exc = task.exception()
            raise RuntimeError(f"gateway servlet failed to start on port {bound_port}: {exc!r}")
        if time.monotonic() > deadline:
            task.cancel()
            raise RuntimeError(f"gateway servlet did not start in time on port {bound_port}")
        await asyncio.sleep(0.02)
    return server, task, bound_port


async def start_gateway_servlet(
    native_catalog_provider: Callable[[], dict[str, Any] | None] | None = None,
    port: int | None = None,
) -> GatewayHandle:
    """
    Start the servlet on loopback and publish its discovery state.

    Port policy (``port=None``): rebind the previous state file's port when
    its owner is dead (session configs freeze the base URL, so a restart must
    come back on the same port), else the fixed default
    :data:`DEFAULT_GATEWAY_PORT`, else an OS-assigned fallback. A state file
    owned by a *live* foreign pid is left alone — a second daemon must not
    fight the first for its port.

    :param native_catalog_provider: See :class:`GatewayServlet`.
    :param port: Explicit port to bind (no fallback), or ``None`` for the
        policy above.
    :returns: A handle owning the listener.
    :raises RuntimeError: When no candidate port can be bound.
    """
    servlet = GatewayServlet(native_catalog_provider)
    app = servlet.build_app()
    if port is not None:
        candidates = [port]
    else:
        candidates = []
        prior = read_servlet_state(allow_stale=True)
        if prior is not None:
            prior_port = _port_of(prior.url)
            if _pid_alive(prior.pid) and prior.pid != os.getpid():
                # A second daemon must not fight the first for its port —
                # and must not overwrite the shared state file either, which
                # would redirect new registrations away from the live owner.
                raise RuntimeError(
                    f"gateway servlet already running at {prior.url} "
                    f"(pid {prior.pid}); refusing to start a second one"
                )
            if prior_port is not None:
                candidates.append(prior_port)
        if DEFAULT_GATEWAY_PORT not in candidates:
            candidates.append(DEFAULT_GATEWAY_PORT)
        candidates.append(0)
    last_error: Exception | None = None
    server: uvicorn.Server | None = None
    task: asyncio.Task[None] | None = None
    bound_port = 0
    for candidate in candidates:
        try:
            server, task, bound_port = await _serve_on(app, candidate)
            break
        except RuntimeError as exc:
            last_error = exc
            _logger.warning("gateway servlet could not use port %s: %s", candidate, exc)
    if server is None or task is None:
        raise RuntimeError(f"gateway servlet failed to start: {last_error}")
    url = f"http://127.0.0.1:{bound_port}"
    write_servlet_state(ServletState(url=url, admin_token=servlet.admin_token, pid=os.getpid()))
    return GatewayHandle(url=url, servlet=servlet, _server=server, _task=task)
