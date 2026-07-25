"""Server-side WebSocket endpoint for runner tunnels (Phase 4/10).

Runners behind NAT connect here via outbound WebSocket. The server
pushes framed HTTP requests over the tunnel; the runner's ASGI
adapter dispatches them and frames responses back.

Per ``designs/RUNNER.md`` §2, the runner sends a ``hello`` frame
on connect advertising its version, harness capabilities, and env
types. The server validates ``frame_protocol_version`` for version-
skew enforcement (strict-major, loose-minor per §2 "Version skew").

The endpoint registers the runner in the :class:`TunnelRegistry`
which the :class:`WSTunnelTransport` uses to route requests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from ipaddress import ip_address

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runner.transports.ws_tunnel.frames import (
    PingFrame,
    PongFrame,
    WSCloseFrame,
    WSFrame,
    decode_frame,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.registry import RunnerSession, TunnelRegistry
from omnigent.server import session_live_state
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.host_registry import RunnerExitReports
from omnigent.server.routes._auth_helpers import require_user

_logger = logging.getLogger(__name__)

SUPPORTED_FRAME_PROTOCOL_MAJOR = 1
PING_INTERVAL_S = 30.0
PING_MISS_THRESHOLD = 3
RUNNER_ID_MISMATCH_CLOSE_CODE = 4004
_ON_RUNNER_CONNECT_TIMEOUT_SEC = 30.0

# Lifetime of a managed runner's minted owner bearer (POST
# /v1/runners/{id}/token). Short by design: the runner re-mints on demand
# via its token factory, so a compromised sandbox's credential is usable
# only briefly, while a live session refreshes indefinitely with no cap.
_MANAGED_RUNNER_TOKEN_TTL_S = 1800


def _is_loopback_websocket_client(ws: WebSocket) -> bool:
    """Return whether the WebSocket peer is a loopback client.

    Local ``omnigent server`` starts an unauthenticated runner that
    connects over loopback. Remote ``run --server`` runners reach
    shared App servers through the auth proxy and must present the
    tunnel binding token.

    :param ws: Incoming WebSocket connection.
    :returns: ``True`` when the peer host is loopback, otherwise
        ``False``.
    """
    if ws.client is None:
        return False
    host = ws.client.host
    if host == "localhost":
        return True
    try:
        addr = ip_address(host)
    except ValueError:
        return False
    mapped_ipv4 = getattr(addr, "ipv4_mapped", None)
    return addr.is_loopback or (mapped_ipv4 is not None and mapped_ipv4.is_loopback)


def _resolve_tunnel_owner(
    ws: WebSocket,
    *,
    auth_provider: AuthProvider | None,
    is_loopback: bool,
) -> str | None:
    """Resolve the owner identity for a runner tunnel handshake.

    Reads the authenticated identity from the WebSocket handshake
    (headers / cookies, available before ``accept()``) and applies the
    single-user loopback remap. The caller is responsible for failing
    closed when this returns ``None`` while ``auth_provider`` is set —
    that combination means auth is enabled but the non-loopback peer did
    not authenticate.

    :param ws: Incoming WebSocket connection whose handshake carries
        the caller's credentials.
    :param auth_provider: Active auth provider, or ``None`` for a
        single-user / no-auth deployment.
    :param is_loopback: Whether the peer is a loopback client, e.g.
        the unauthenticated local runner started by ``omnigent server``.
    :returns: The owner user id, e.g. ``"alice@example.com"``;
        :data:`RESERVED_USER_LOCAL` for an unauthenticated loopback
        peer; or ``None`` when auth is enabled and a non-loopback peer
        presented no identity (auth failure — caller must reject) or
        when no auth provider is configured (single-user mode).
    """
    owner: str | None = None
    if auth_provider is not None:
        owner = auth_provider.get_user_id(ws)
    if owner is None and is_loopback:
        # Local runner: assign the single-user identity so ownership
        # checks work uniformly in single-user mode.
        owner = RESERVED_USER_LOCAL
    return owner


def _expected_runner_id_from_headers(
    headers: Mapping[str, str],
    *,
    allowed_tunnel_tokens: frozenset[str] | None = None,
) -> str | None:
    """Return the runner id authorized by WebSocket tunnel headers.

    Servers with an allow-list require the token to be present in that
    allow-list and may use stable runner ids. Servers without an
    allow-list accept token-bound runner ids, while loopback clients
    may omit the token for legacy local development flows.

    :param headers: WebSocket handshake headers.
    :param allowed_tunnel_tokens: Optional set of accepted binding
        tokens, e.g. ``frozenset({"uA6Zz..."})``.
    :returns: Expected token-bound runner id, or ``None`` when the
        token directly authorizes the path runner id.
    :raises ValueError: If the tunnel token header is missing when
        required, present but empty, or not authorized.
    """
    token = headers.get(RUNNER_TUNNEL_TOKEN_HEADER)
    if token is None:
        if allowed_tunnel_tokens is not None:
            raise ValueError("runner tunnel token is required")
        return None
    stripped = token.strip()
    if not stripped:
        raise ValueError("runner tunnel token must not be empty")
    if allowed_tunnel_tokens is not None:
        if stripped not in allowed_tunnel_tokens:
            raise ValueError("runner tunnel token is not authorized")
        return None
    return token_bound_runner_id(stripped)


def create_runner_tunnel_router(
    registry: TunnelRegistry,
    *,
    allowed_tunnel_tokens: frozenset[str] | None = None,
    on_runner_disconnect: Callable[[str], Awaitable[None]] | None = None,
    on_runner_connect: Callable[[str], Awaitable[None]] | None = None,
    auth_provider: AuthProvider | None = None,
    runner_exit_reports: RunnerExitReports | None = None,
    resolve_managed_runner_owner: Callable[[str], str | None] | None = None,
) -> APIRouter:
    """Build the router hosting the ``/runners/{id}/tunnel`` WS endpoint.

    The router is intended to be mounted with ``prefix="/v1"`` (see
    ``server/app.py``) so the final path is ``/v1/runners/{id}/tunnel``.

    :param registry: The server's :class:`TunnelRegistry` instance.
        Shared with the :class:`WSTunnelTransport` that routes
        requests through registered runners.
    :param allowed_tunnel_tokens: Optional set of accepted binding
        tokens, e.g. ``frozenset({"uA6Zz..."})``. ``None`` keeps
        shared remote-server behavior by accepting any token-bound
        runner id.
    :param on_runner_disconnect: Optional async callback fired when
        a runner's tunnel closes. Receives the ``runner_id``. Used
        by the sessions module to mark sessions ``runner_offline``.
    :param on_runner_connect: Optional async callback fired when a
        runner tunnel is established. Receives the ``runner_id``.
        Used to re-assign sessions on runner reconnect.
    :param auth_provider: Optional auth provider for user identity
        extraction. When set, runner listing is scoped to the
        caller's own runners and tunnel registration records
        the authenticated owner.
    :param runner_exit_reports: Exit reports recorded by the host
        tunnel (``host.runner_exited``). When set, the status
        endpoint includes the failure cause for a runner that died
        before (or after) connecting, so waiting clients fail fast
        instead of polling to a timeout. ``None`` (e.g. minimal test
        wiring, or a server without host support) omits the field.
    :param resolve_managed_runner_owner: Optional ``runner_id -> owner``
        resolver for host-launched and managed-sandbox runners. A delegated
        runner authenticates with a binding token (not a user session), so
        ``auth_provider.get_user_id`` cannot resolve it;
        this looks up the owner the server recorded for the runner at
        launch (the conversation bound to ``runner_id``) — the
        runner-side analog of the host tunnel's ``resolve_launch_token``.
        ``None`` disables the lookup (an unauthenticated non-loopback
        peer is then rejected, the prior behavior).
    :returns: A FastAPI router with the tunnel endpoint.
    """
    router = APIRouter()

    def _get_user_id_from_request(request: Request) -> str | None:
        """Extract user identity from an HTTP request, 401 if rejected.

        Delegates to :func:`require_user`: when an auth provider is
        configured, an unauthenticated request raises 401 instead of
        resolving to ``None`` — a ``None`` user would skip the
        ownership scoping below and expose every runner. ``None`` is
        returned only when auth is disabled entirely.

        :param request: The incoming FastAPI request.
        :returns: User ID string, or ``None`` if no auth provider.
        :raises OmnigentError: 401 when the provider rejects the
            request.
        """
        return require_user(request, auth_provider)

    @router.get("/runners")
    async def list_runners(request: Request) -> dict[str, list[dict[str, object]]]:
        """
        Return currently online runners owned by the requesting user.

        When auth is active, only runners whose tunnel was
        established by the same user are returned. Without auth,
        all online runners are listed (single-user / dev mode).

        :param request: The incoming FastAPI request (for auth).
        :returns: A ``{"data": [...]}`` list with runner ids and
            advertised harnesses.
        """
        user_id = _get_user_id_from_request(request)
        data: list[dict[str, object]] = []
        for runner_id in registry.online_runner_ids():
            session = registry.get(runner_id)
            if session is None:
                continue
            # Scope listing to the caller's own runners.
            if user_id is not None and session.owner is not None and session.owner != user_id:
                continue
            data.append(
                {
                    "runner_id": runner_id,
                    "online": True,
                    "harnesses": list(session.hello.harnesses),
                }
            )
        return {"data": data}

    @router.get("/runners/{runner_id}/status")
    async def runner_status(request: Request, runner_id: str) -> dict[str, str | bool]:
        """Return whether a runner currently has an open tunnel.

        When auth is active, a runner owned by a different user
        appears as offline to prevent enumeration.

        :param request: The incoming FastAPI request (for auth).
        :param runner_id: Stable runner id, e.g.
            ``"runner_0123456789abcdef"``.
        :returns: A JSON object with ``runner_id`` and ``online``.
        """
        user_id = _get_user_id_from_request(request)
        session = registry.get(runner_id)
        online = session is not None
        # Hide runners owned by other users.
        if (
            online
            and user_id is not None
            and session.owner is not None
            and session.owner != user_id
        ):
            online = False
        result: dict[str, str | bool] = {"runner_id": runner_id, "online": online}
        if not online and runner_exit_reports is not None:
            # Host-daemon report that the runner process died (exit code
            # + log tail). Owner-scoped inside get_visible, so other
            # users' runners reveal nothing (same W6-2 posture as above).
            error = runner_exit_reports.get_visible(runner_id, user_id)
            if error is not None:
                result["error"] = error
        return result

    @router.post("/runners/{runner_id}/token")
    async def mint_runner_owner_token(request: Request, runner_id: str) -> dict[str, str | int]:
        """Mint a short-lived owner bearer for a delegated runner.

        A host-launched or managed-sandbox runner does not inherit the host
        user's credential; it presents its server-minted tunnel binding token
        (``X-Omnigent-Runner-Tunnel-Token``) and the server returns a
        short-lived owner JWT the runner then uses on its HTTP callbacks
        (which gate on ``require_user``). This is the HTTP analog of the
        runner tunnel's binding-token handshake: the same SHA-256 gate
        (``token_bound_runner_id(token) == runner_id``) and the same
        owner resolution (``resolve_managed_runner_owner``), minting a
        bearer instead of registering a tunnel.

        The binding-token match is required unconditionally — the
        allow-list shortcut honored on some other runner-token checks is
        deliberately NOT accepted here, because this endpoint issues a
        full owner credential and delegated runners are token-bound.

        :param request: The incoming FastAPI request (carries the binding
            token header).
        :param runner_id: Token-bound runner id from the path.
        :returns: ``{"token": <jwt>, "expires_at": <epoch seconds>}``.
        :raises OmnigentError: 401 when the binding token is absent,
            doesn't match ``runner_id``, or resolves to no managed-launch
            owner; 400 when the active auth mode can't mint server-side
            (header/proxy, or no auth provider).
        """
        if auth_provider is None:
            # No auth configured: the runner authenticates by binding
            # token alone and needs no bearer — minting is meaningless.
            raise OmnigentError(
                "runner token minting requires an auth provider",
                code=ErrorCode.INVALID_INPUT,
            )
        token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
        if not token or token_bound_runner_id(token) != runner_id:
            raise OmnigentError("unauthenticated", code=ErrorCode.UNAUTHORIZED)
        owner: str | None = None
        if resolve_managed_runner_owner is not None:
            owner = await asyncio.to_thread(resolve_managed_runner_owner, runner_id)
        if owner is None:
            # No managed-launch record bound to this runner id: a peer
            # with a syntactically valid but unrecognized token. Refuse,
            # the same fail-closed posture as the tunnel handshake.
            raise OmnigentError("unauthenticated", code=ErrorCode.UNAUTHORIZED)
        bearer = auth_provider.mint_runner_token(owner, _MANAGED_RUNNER_TOKEN_TTL_S)
        if bearer is None:
            # oidc/accounts mint; header/proxy mode can't (identity is
            # asserted upstream). Signal clearly rather than 401.
            raise OmnigentError(
                "runner token minting is unsupported in this auth mode",
                code=ErrorCode.INVALID_INPUT,
            )
        return {
            "token": bearer,
            "expires_at": int(time.time()) + _MANAGED_RUNNER_TOKEN_TTL_S,
        }

    @router.websocket("/runners/{runner_id}/tunnel")
    async def tunnel(ws: WebSocket, runner_id: str) -> None:
        """Accept a runner's outbound WebSocket tunnel.

        Protocol:
        1. Validate the tunnel token and resolve the owner from the
           handshake; refuse an unauthenticated non-loopback peer with
           4004 BEFORE accepting the upgrade.
        2. Accept the WS upgrade.
        3. Receive the ``hello`` frame.
        4. Validate ``frame_protocol_version`` (strict-major).
        5. Register in the TunnelRegistry under the resolved owner.
        6. Start a ping loop (keepalive).
        7. Loop receiving frames; route response frames via
           ``registry.route_response_frame()``.
        8. On disconnect: deregister, abort in-flight.
        """
        is_loopback = _is_loopback_websocket_client(ws)
        try:
            expected_runner_id = _expected_runner_id_from_headers(
                ws.headers,
                allowed_tunnel_tokens=allowed_tunnel_tokens,
            )
        except ValueError as exc:
            if not is_loopback:
                await ws.accept()
                await ws.close(
                    code=RUNNER_ID_MISMATCH_CLOSE_CODE,
                    reason=str(exc),
                )
                return
            # Loopback fallback: the token wasn't in the server's
            # allow-list (external ``run --server`` runner). Retry
            # without the allow-list so token-bound ID derivation
            # kicks in instead of rejecting.
            expected_runner_id = _expected_runner_id_from_headers(
                ws.headers,
                allowed_tunnel_tokens=None,
            )
        if expected_runner_id is None and allowed_tunnel_tokens is None and not is_loopback:
            await ws.accept()
            await ws.close(
                code=RUNNER_ID_MISMATCH_CLOSE_CODE,
                reason="runner tunnel token is required",
            )
            return
        if expected_runner_id is not None and runner_id != expected_runner_id:
            await ws.accept()
            await ws.close(
                code=RUNNER_ID_MISMATCH_CLOSE_CODE,
                reason="runner_id does not match tunnel token",
            )
            return

        # Resolve the tunnel owner from the handshake and fail
        # closed for an unauthenticated non-loopback peer BEFORE accepting
        # the upgrade — mirror host_tunnel.py's behavior (no
        # acceptance oracle, no pre-auth protocol I/O). ``get_user_id``
        # reads only the handshake headers/cookies, which Starlette
        # exposes before ``accept()``.
        #
        # The token-binding gate above only proves the peer knows *a*
        # token; in no-allowlist mode (``allowed_tunnel_tokens is None``,
        # the standard deployed posture — see ``cli.py``) any
        # attacker-chosen non-empty token derives a valid runner id and
        # clears that gate. It never establishes a user identity.
        # Registering with ``owner=None`` would then bypass BOTH
        # owner-scoped guards: the listing filter and the
        # session-binding check each skip enforcement when the runner's
        # ``owner is None``, so an owner-less runner becomes visible to —
        # and bindable by — every other tenant.
        tunnel_owner = _resolve_tunnel_owner(
            ws,
            auth_provider=auth_provider,
            is_loopback=is_loopback,
        )
        if tunnel_owner is None and auth_provider is not None:
            # A server-managed sandbox runner authenticates with a
            # server-minted binding token (the token-binding gate above
            # already proved ``token_bound_runner_id(token) == runner_id``),
            # not a user cookie / Bearer — so ``get_user_id`` returns None
            # here even though the peer is legitimate. Resolve the owner the
            # server recorded for this runner at launch (the conversation
            # bound to ``runner_id``), mirroring the host tunnel's
            # ``resolve_launch_token`` path. Only a peer holding the real
            # 32-byte binding token reaches this branch, so an
            # attacker-chosen token cannot map to a victim's runner_id.
            if resolve_managed_runner_owner is not None:
                tunnel_owner = await asyncio.to_thread(resolve_managed_runner_owner, runner_id)
            if tunnel_owner is None:
                # No managed-launch record either: a genuinely
                # unauthenticated non-loopback peer. Refuse the handshake
                # instead of registering an owner-less runner.
                await ws.close(code=RUNNER_ID_MISMATCH_CLOSE_CODE, reason="unauthenticated")
                return

        await ws.accept()
        session: RunnerSession | None = None
        try:
            # 3. Receive hello frame.
            raw = await ws.receive_text()
            frame = decode_frame(raw)
            if not hasattr(frame, "frame_protocol_version"):
                await ws.close(code=4001, reason="expected hello frame")
                return

            # 4. Version-skew check (strict-major).
            remote_major = frame.frame_protocol_version
            if remote_major != SUPPORTED_FRAME_PROTOCOL_MAJOR:
                await ws.close(
                    code=4002,
                    reason=(
                        f"frame_protocol_version mismatch: "
                        f"server supports {SUPPORTED_FRAME_PROTOCOL_MAJOR}, "
                        f"runner sent {remote_major}"
                    ),
                )
                return

            # 5. Register — the authenticated tunnel owner was resolved
            #    (and an unauthenticated non-loopback peer already
            #    rejected) before ``accept()`` above, so runner-binding
            #    checks can enforce ownership.
            session = registry.register(runner_id, ws, frame, owner=tunnel_owner)
            _logger.info(
                "Runner %s connected (version=%s, harnesses=%s)",
                runner_id,
                frame.runner_version,
                frame.harnesses,
            )

            # 6. Start tunnel helper tasks. The sender task is the
            # only code path that writes to the Starlette WebSocket;
            # request-side callers enqueue frames through the registry.
            # These start BEFORE ``on_runner_connect`` fires so the
            # hook can perform real tunnel I/O — without the sender
            # loop running, any ``WSTunnelTransport``-backed request
            # the hook makes would deadlock on its response future.
            sender_task = asyncio.create_task(
                _sender_loop(ws, session),
                name=f"tunnel-sender:{runner_id}",
            )
            ping_task = asyncio.create_task(
                _ping_loop(ws, session, runner_id, registry),
                name=f"tunnel-ping:{runner_id}",
            )
            receive_task = asyncio.create_task(
                _receive_loop(ws, session, runner_id, registry),
                name=f"tunnel-receive:{runner_id}",
            )

            if on_runner_connect is not None:
                # Bounded so a slow / hung hook can't stall WS
                # shutdown: helper tasks are already running, but
                # we still await the hook before entering the
                # tunnel's main wait loop.
                try:
                    await asyncio.wait_for(
                        on_runner_connect(runner_id),
                        timeout=_ON_RUNNER_CONNECT_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    _logger.warning(
                        "on_runner_connect callback timed out for %s after %ss",
                        runner_id,
                        _ON_RUNNER_CONNECT_TIMEOUT_SEC,
                    )
                except Exception:
                    _logger.exception(
                        "on_runner_connect callback failed for %s",
                        runner_id,
                    )

            try:
                done, _pending = await asyncio.wait(
                    {sender_task, ping_task, receive_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    task_name = task.get_name()
                    if task.cancelled():
                        _logger.warning(
                            "Tunnel helper task cancelled for runner %s: %s",
                            runner_id,
                            task_name,
                        )
                        continue
                    exc = task.exception()
                    if exc is None:
                        _logger.warning(
                            "Tunnel helper task ended for runner %s: %s",
                            runner_id,
                            task_name,
                        )
                        continue
                    if isinstance(exc, WebSocketDisconnect):
                        _logger.warning(
                            "Tunnel helper task disconnected for runner %s: %s "
                            "(code=%s, reason=%r)",
                            runner_id,
                            task_name,
                            getattr(exc, "code", None),
                            getattr(exc, "reason", None),
                        )
                    else:
                        _logger.warning(
                            "Tunnel helper task failed for runner %s: %s",
                            runner_id,
                            task_name,
                            exc_info=(type(exc), exc, exc.__traceback__),
                        )
                    raise exc
            finally:
                for task in (sender_task, ping_task, receive_task):
                    task.cancel()
                await asyncio.gather(
                    sender_task,
                    ping_task,
                    receive_task,
                    return_exceptions=True,
                )
                registry.deregister(runner_id, session)
                if on_runner_disconnect is not None:
                    try:
                        await on_runner_disconnect(runner_id)
                    except Exception:
                        _logger.exception(
                            "on_runner_disconnect callback failed for %s",
                            runner_id,
                        )

        except WebSocketDisconnect as exc:
            _logger.warning(
                "Runner %s websocket disconnected (code=%s, reason=%r)",
                runner_id,
                getattr(exc, "code", None),
                getattr(exc, "reason", None),
            )
            if on_runner_disconnect is not None:
                try:
                    await on_runner_disconnect(runner_id)
                except Exception:
                    _logger.exception(
                        "on_runner_disconnect callback failed for %s",
                        runner_id,
                    )
        except Exception:
            _logger.exception("Tunnel error for runner %s", runner_id)
            if session is not None:
                registry.deregister(runner_id, session)
            else:
                registry.deregister(runner_id)
            if on_runner_disconnect is not None:
                try:
                    await on_runner_disconnect(runner_id)
                except Exception:
                    _logger.exception(
                        "on_runner_disconnect callback failed for %s",
                        runner_id,
                    )

    return router


async def _sender_loop(ws: WebSocket, session: RunnerSession) -> None:
    """Send queued frames on the WebSocket owner loop.

    :param ws: Accepted Starlette WebSocket.
    :param session: Current runner session whose queue this task
        drains.
    :returns: None when the session is retired.
    """
    while True:
        data = await session.outbound_queue.get()
        if data is None:
            return
        await ws.send_text(data)


async def _receive_tunnel_text(ws: WebSocket, runner_id: str) -> str | None:
    """Receive one tunnel text message.

    :param ws: Accepted Starlette WebSocket.
    :param runner_id: Runner id for logging, e.g.
        ``"runner_0123456789abcdef"``.
    :returns: Text payload, or ``None`` for a non-text frame.
    :raises WebSocketDisconnect: If the client disconnected.
    """
    message = await ws.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(
            code=message.get("code", 1000),
            reason=message.get("reason"),
        )
    raw = message.get("text")
    if not isinstance(raw, str):
        _logger.warning("Runner %s sent non-text tunnel frame; dropping", runner_id)
        return None
    return raw


async def _receive_loop(
    ws: WebSocket,
    session: RunnerSession,
    runner_id: str,
    registry: TunnelRegistry,
) -> None:
    """Receive runner frames and route response frames.

    Malformed frames (bad JSON, unknown ``kind``, missing required
    fields, binary payloads) are logged and skipped.

    :param ws: Accepted Starlette WebSocket.
    :param session: Session-generation guard for incoming frames.
    :param runner_id: Runner id for logging and registry lookup,
        e.g. ``"runner_0123456789abcdef"``.
    :param registry: Tunnel registry shared with the transport.
    :returns: None when the WebSocket disconnects or the session is
        no longer current.
    """
    while True:
        raw = await _receive_tunnel_text(ws, runner_id)
        if not registry.mark_frame_seen(session):
            return
        if raw is None:
            continue
        try:
            resp_frame = decode_frame(raw)
        except ValueError as exc:
            _logger.warning(
                "Runner %s sent malformed tunnel frame; dropping: %s",
                runner_id,
                exc,
            )
            continue
        if isinstance(resp_frame, PongFrame):
            # Tunnel keepalive round-trip. DEBUG because pings are frequent —
            # opt in via log level. ``ts`` is epoch-ms stamped when the server
            # pinged, so now - ts is the runner round-trip latency.
            _logger.debug(
                "runner %s tunnel keepalive: pong rtt=%dms",
                runner_id,
                int(time.time() * 1000) - resp_frame.ts,
            )
            continue
        if isinstance(resp_frame, (WSFrame, WSCloseFrame)):
            registry.route_ws_inbound(runner_id, resp_frame, session=session)
            continue
        # Route response.* frames to the reassembly queue.
        registry.route_response_frame(runner_id, resp_frame, session=session)


async def _ping_loop(
    ws: WebSocket,
    session: RunnerSession,
    runner_id: str,
    registry: TunnelRegistry,
) -> None:
    """Send pings every PING_INTERVAL_S; declare dead after misses.

    Each tick that the runner is still alive also re-stamps
    ``runner_last_seen`` (``session_live_state.touch_runner_liveness``)
    so replicas that don't hold this tunnel keep deriving
    ``runner_online`` from a fresh row instead of their own empty
    registry. This runs from the per-connection ping loop — inside the
    tunnel handler's ``workspace_scope`` — rather than a central lifespan
    sweep, which would run context-free (default workspace) over a
    workspace-blind registry and stamp no rows on a multi-tenant replica.
    Mirrors the host tunnel's ``host_store.heartbeat`` refresh.

    :param ws: Accepted Starlette WebSocket used only for timeout
        close.
    :param session: Session-generation guard for the ping loop.
    :param runner_id: Runner id for logging and registry lookup,
        e.g. ``"runner_0123456789abcdef"``.
    :param registry: Tunnel registry shared with the transport.
    :returns: None when the session goes stale or the runner is
        declared dead.
    """
    while True:
        await asyncio.sleep(PING_INTERVAL_S)
        # Check if any frame arrived recently (not just pongs).
        elapsed = registry.seconds_since_last_frame(session)
        if elapsed is None:
            return
        if elapsed > PING_INTERVAL_S * PING_MISS_THRESHOLD:
            _logger.warning(
                "Runner %s missed %d ping intervals (%.0fs since last frame); declaring dead",
                runner_id,
                PING_MISS_THRESHOLD,
                elapsed,
            )
            try:
                await ws.close(code=4003, reason="ping timeout")
            except RuntimeError:
                _logger.debug("Runner %s websocket already closed during ping timeout", runner_id)
            return
        # Still within the liveness window — refresh the row so the
        # freshness gate keeps the runner in the online set cross-replica.
        # Best-effort and deduplicated inside the chokepoint; the enqueue
        # inherits this handler's workspace scope via copy_context.
        session_live_state.touch_runner_liveness([runner_id])
        try:
            await registry.send_text(
                session,
                encode_frame(PingFrame(ts=int(time.time() * 1000))),
            )
        except Exception:  # noqa: BLE001 -- any send failure ends the ping loop cleanly.
            return
