"""Server-side WebSocket endpoint for host tunnels.

Hosts (machines running ``omnigent host``) connect here via
outbound WebSocket. The server sends control frames
(launch/stop runner) over the tunnel; the host process spawns
or terminates runner subprocesses accordingly.

Per ``designs/DAEMON_API.md``, the host sends a ``host.hello``
frame on connect advertising its version, name, and live runner
IDs. The server validates ``frame_protocol_version`` for
version-skew enforcement (strict-major).

The endpoint registers the host in the :class:`HostRegistry`
(in-memory, per-replica) and upserts the host in the ``hosts``
DB table (cross-replica, persistent).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from omnigent.host.frames import (
    HostCreateDirResultFrame,
    HostCreateWorktreeResultFrame,
    HostHelloFrame,
    HostLaunchRunnerResultFrame,
    HostListDirResultFrame,
    HostRemoveWorktreeResultFrame,
    HostRunnerExitedFrame,
    HostStatResultFrame,
    HostStopRunnerResultFrame,
    decode_host_frame,
)
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.runner.transports.ws_tunnel.frames import (
    PingFrame,
    PongFrame,
    decode_frame,
    encode_frame,
)
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.host_registry import (
    HostConnection,
    HostRegistry,
    RunnerExitReports,
)
from omnigent.stores.host_store import HostStore

_logger = logging.getLogger(__name__)

SUPPORTED_FRAME_PROTOCOL_MAJOR = 1
PING_INTERVAL_S = 30.0
PING_MISS_THRESHOLD = 3


def create_host_tunnel_router(
    host_registry: HostRegistry,
    host_store: HostStore,
    *,
    auth_provider: AuthProvider | None = None,
    on_host_connect: Callable[[str], Awaitable[None]] | None = None,
    on_host_disconnect: Callable[[str], Awaitable[None]] | None = None,
    on_runner_exited: Callable[[str, str], Awaitable[None]] | None = None,
    local_single_user: bool | None = None,
    runner_exit_reports: RunnerExitReports | None = None,
) -> APIRouter:
    """Build the router hosting the ``/hosts/{id}/tunnel`` WS endpoint.

    Mounted with ``prefix="/v1"`` so the final path is
    ``/v1/hosts/{host_id}/tunnel``.

    :param host_registry: In-memory registry of live host
        connections on this replica.
    :param host_store: Persistent store for host registrations
        (cross-replica). Also the credential source for SERVER-MANAGED
        sandbox hosts: a connection presenting the managed-host token
        header authenticates via
        :meth:`HostStore.resolve_launch_token` instead of
        *auth_provider* (sandboxes have no user credentials).
    :param auth_provider: Optional auth provider for user identity.
        When set, the authenticated user is recorded as the host
        owner.
    :param on_host_connect: Optional async callback fired after a
        host's tunnel is established. Receives the ``host_id``.
        Used for reconnect reconciliation.
    :param on_runner_exited: Optional async callback fired when a host
        reports one of its spawned runners died unexpectedly
        (``host.runner_exited``). Receives ``(runner_id, error)``.
        The server wires this to mark the runner's session(s) failed
        and push the cause to the open view — the only failure signal
        for a runner that crashed before connecting its tunnel (so the
        runner-tunnel ``on_runner_disconnect`` path never fires).
    :param on_host_disconnect: Optional async callback fired when
        a host's tunnel closes. Receives the ``host_id``.
    :param local_single_user: When ``True``, allow a host to re-own a
        ``host_id`` already registered under a different owner — needed
        only for the single-user loopback local server, where the owner
        legitimately changes across an accounts↔header auth-mode flip.
        ``None`` (the default) resolves from ``OMNIGENT_LOCAL_SINGLE_USER``
        so the deployed multi-user server (which never sets it) keeps the
        W2-class host-hijack boundary. Tests pass an explicit bool.
    :param runner_exit_reports: Shared store for ``host.runner_exited``
        reports, read by the runner status endpoint. ``None`` (e.g.
        minimal test wiring) drops the reports.
    :returns: A FastAPI router with the host tunnel endpoint.
    """
    from omnigent.server.auth import local_single_user_enabled

    allow_host_id_reown = (
        local_single_user if local_single_user is not None else local_single_user_enabled()
    )
    router = APIRouter()

    @router.websocket("/hosts/{host_id}/tunnel")
    async def tunnel(ws: WebSocket, host_id: str) -> None:
        """Accept a host's outbound WebSocket tunnel.

        Protocol:
        1. Authenticate the owner from the handshake (BEFORE accept).
        2. Accept the WS upgrade.
        3. Receive the ``host.hello`` frame.
        4. Validate ``frame_protocol_version`` (strict-major).
        5. Upsert in the ``hosts`` DB table.
        6. Register in the :class:`HostRegistry`.
        7. Start sender, receiver, and ping loops.
        8. On disconnect: deregister, set offline in DB.
        """
        # Authenticate from the handshake BEFORE accepting the upgrade,
        # so an unauthenticated peer never completes the WS handshake — no
        # acceptance oracle and no pre-auth protocol I/O. ``get_user_id`` reads
        # only the handshake headers/cookies, which Starlette exposes before
        # ``accept()``.
        managed_token = ws.headers.get(MANAGED_HOST_TOKEN_HEADER)
        if managed_token is not None:
            # A managed-host launch token is an explicit credential: when
            # presented, it must resolve — never fall through to user auth
            # (a peer that chose this header has no user identity to fall
            # back on, and falling back would let a junk token downgrade
            # into header/anonymous auth). The token is scoped to one
            # host_id; presenting it for any other path fails closed so a
            # leaked token cannot register arbitrary hosts.
            managed = await asyncio.to_thread(host_store.resolve_launch_token, managed_token)
            if managed is None or managed.host_id != host_id:
                await ws.close(code=4004, reason="unauthenticated")
                return
            tunnel_owner = managed.owner
        elif auth_provider is not None:
            tunnel_owner = auth_provider.get_user_id(ws)
            if tunnel_owner is None:
                # Auth is enabled but this peer didn't authenticate. Fail
                # closed — never fall back to RESERVED_USER_LOCAL, which is
                # admin-equivalent under the multi-user header scheme
                # Closing before accept() refuses the handshake.
                await ws.close(code=4004, reason="unauthenticated")
                return
        else:
            # No auth provider configured = explicit single-user / local
            # deployment; RESERVED_USER_LOCAL is the accepted local owner
            # (consistent with get_user_id returning None on the HTTP side).
            tunnel_owner = RESERVED_USER_LOCAL

        # Reject a cross-owner takeover before accept(). ``host_id`` is
        # UNIQUE, so a peer authenticated as one user dialing in on a
        # host_id owned by another collides inside ``upsert_on_connect`` —
        # but only AFTER accept(), as an opaque IntegrityError that drops
        # the tunnel post-handshake while the host keeps printing
        # "✓ Connected" and reconnect-loops. Catching it here makes the
        # refusal clean and fatal. Skipped for the single-user local server
        # (allow_host_id_reown re-owns in place); the IntegrityError stays
        # the backstop for the connect/connect race this can't lock.
        if not allow_host_id_reown:
            existing = await asyncio.to_thread(host_store.get_host, host_id)
            if existing is not None and existing.owner != tunnel_owner:
                _logger.warning(
                    "Refusing host %s: registered to owner %r but the "
                    "connecting peer authenticated as %r. Cross-owner "
                    "re-registration is not allowed — remove the stale "
                    "registration or reset the host id.",
                    host_id,
                    existing.owner,
                    tunnel_owner,
                )
                # Don't name the existing owner to this peer: in a multi-user
                # server that discloses another account's identity. The log
                # above carries the detail for the operator.
                await _refuse_upgrade(
                    ws,
                    status=409,
                    reason=(
                        "This machine is already registered to a different "
                        "account on this server. An administrator must remove "
                        "the existing host registration, or reset this host's "
                        "id, before it can reconnect."
                    ),
                )
                return

        await ws.accept()
        conn: HostConnection | None = None
        try:
            raw = await ws.receive_text()
            frame = decode_host_frame(raw)
            if not isinstance(frame, HostHelloFrame):
                await ws.close(code=4001, reason="expected host.hello frame")
                return

            remote_major = frame.frame_protocol_version
            if remote_major != SUPPORTED_FRAME_PROTOCOL_MAJOR:
                await ws.close(
                    code=4002,
                    reason=(
                        f"frame_protocol_version mismatch: "
                        f"server supports {SUPPORTED_FRAME_PROTOCOL_MAJOR}, "
                        f"host sent {remote_major}"
                    ),
                )
                return

            await asyncio.to_thread(
                host_store.upsert_on_connect,
                host_id=host_id,
                name=frame.name,
                owner=tunnel_owner,
                allow_host_id_reown=allow_host_id_reown,
                configured_harnesses=frame.configured_harnesses,
            )

            conn = host_registry.register(
                host_id,
                ws,
                frame,
                owner=tunnel_owner,
            )
            _logger.info(
                "Host %s connected (version=%s, name=%s, runners=%s)",
                host_id,
                frame.version,
                frame.name,
                frame.runners,
            )

            sender_task = asyncio.create_task(
                _sender_loop(ws, conn),
                name=f"host-sender:{host_id}",
            )
            ping_task = asyncio.create_task(
                _ping_loop(ws, conn, host_id, host_store),
                name=f"host-ping:{host_id}",
            )
            receive_task = asyncio.create_task(
                _receive_loop(ws, conn, host_id, runner_exit_reports, on_runner_exited),
                name=f"host-receive:{host_id}",
            )

            if on_host_connect is not None:
                try:
                    await asyncio.wait_for(
                        on_host_connect(host_id),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    _logger.warning(
                        "on_host_connect callback timed out for %s",
                        host_id,
                    )
                except Exception:
                    _logger.exception(
                        "on_host_connect callback failed for %s",
                        host_id,
                    )

            try:
                done, _pending = await asyncio.wait(
                    {sender_task, ping_task, receive_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    exc = task.exception() if not task.cancelled() else None
                    if exc is not None:
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
                host_registry.deregister(host_id)
                await asyncio.to_thread(host_store.set_offline, host_id)
                if on_host_disconnect is not None:
                    try:
                        await on_host_disconnect(host_id)
                    except Exception:
                        _logger.exception(
                            "on_host_disconnect callback failed for %s",
                            host_id,
                        )

        except WebSocketDisconnect:
            _logger.warning("Host %s disconnected", host_id)
            # Only run disconnect cleanup if we actually registered this
            # host on THIS connection. A connect that failed before
            # register — e.g. the upsert IntegrityError when a peer
            # connects with another owner's host_id — must not deregister
            # or flip that owner's host offline (cross-user DoS).
            if conn is not None:
                host_registry.deregister(host_id)
                await asyncio.to_thread(host_store.set_offline, host_id)
                if on_host_disconnect is not None:
                    try:
                        await on_host_disconnect(host_id)
                    except Exception:
                        _logger.exception(
                            "on_host_disconnect callback failed for %s",
                            host_id,
                        )
        except Exception:
            _logger.exception("Host tunnel error for %s", host_id)
            # Same guard as above: don't touch a host we never registered.
            if conn is not None:
                host_registry.deregister(host_id)
                await asyncio.to_thread(host_store.set_offline, host_id)

    return router


async def _refuse_upgrade(ws: WebSocket, *, status: int, reason: str) -> None:
    """Refuse a WebSocket upgrade before ``accept()`` with a real HTTP status.

    Uses the WebSocket Denial Response extension so the host sees a
    specific status (e.g. ``409``) instead of the generic ``403`` a plain
    pre-accept close yields; falls back to a close when the server doesn't
    advertise the extension.

    :param ws: The unaccepted WebSocket.
    :param status: HTTP status for the denial response, e.g. ``409``.
    :param reason: Explanation, sent as the response body or close reason.
    """
    extensions = ws.scope.get("extensions") or {}
    if "websocket.http.response" in extensions:
        from starlette.responses import PlainTextResponse

        await ws.send_denial_response(PlainTextResponse(reason, status_code=status))
    else:
        await ws.close(code=4009, reason=reason)


async def _sender_loop(ws: WebSocket, conn: HostConnection) -> None:
    """Send queued frames on the WebSocket owner loop.

    :param ws: Accepted Starlette WebSocket.
    :param conn: Host connection whose outbound queue to drain.
    """
    while True:
        data = await conn.outbound_queue.get()
        if data is None:
            return
        await ws.send_text(data)


async def _receive_loop(
    ws: WebSocket,
    conn: HostConnection,
    host_id: str,
    runner_exit_reports: RunnerExitReports | None,
    on_runner_exited: Callable[[str, str], Awaitable[None]] | None,
) -> None:
    """Receive host frames and route results to pending futures.

    :param ws: Accepted Starlette WebSocket.
    :param conn: Host connection for resolving pending requests.
    :param host_id: Host id for logging.
    :param runner_exit_reports: Store for ``host.runner_exited``
        reports; ``None`` drops them.
    :param on_runner_exited: Callback fired with ``(runner_id, error)``
        when a ``host.runner_exited`` frame arrives; ``None`` skips it.
    """
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(
                code=message.get("code", 1000),
                reason=message.get("reason"),
            )
        raw = message.get("text")
        if not isinstance(raw, str):
            continue

        conn.last_frame_at = time.time()

        try:
            frame = decode_host_frame(raw)
        except ValueError:
            # The tunnel multiplexes host frames (host.*) and runner
            # keepalive frames (ping/pong) on the same socket.  Pong
            # replies are expected — the server sends pings via
            # encode_frame(PingFrame(...)) and the host responds with
            # a pong using the runner-tunnel encoding.
            try:
                runner_frame = decode_frame(raw)
            except ValueError as inner_exc:
                _logger.warning(
                    "Host %s sent malformed frame; dropping: %s",
                    host_id,
                    inner_exc,
                )
                continue
            if isinstance(runner_frame, PongFrame):
                # Host-tunnel keepalive round-trip. DEBUG because pings are
                # frequent — opt in via log level. ``ts`` is epoch-ms stamped
                # when the server pinged, so now - ts is the daemon round-trip.
                _logger.debug(
                    "host %s tunnel keepalive: pong rtt=%dms",
                    host_id,
                    int(time.time() * 1000) - runner_frame.ts,
                )
                continue
            _logger.warning(
                "Host %s sent unexpected runner frame; dropping: kind=%s",
                host_id,
                type(runner_frame).__name__,
            )
            continue

        if isinstance(frame, HostLaunchRunnerResultFrame):
            future = conn.pending_launches.pop(frame.request_id, None)
            if future is not None and not future.done():
                future.set_result(
                    {
                        "status": frame.status,
                        "runner_id": frame.runner_id,
                        "error": frame.error,
                        "error_code": frame.error_code,
                    }
                )
            continue

        if isinstance(frame, HostStopRunnerResultFrame):
            future = conn.pending_stops.pop(frame.request_id, None)
            if future is not None and not future.done():
                future.set_result({"status": frame.status, "error": frame.error})
            continue

        if isinstance(frame, HostRunnerExitedFrame):
            # One-way report: a runner this host spawned died
            # unexpectedly. Stash the cause so the runner status
            # endpoint can answer "offline, and here is why" to the
            # client still waiting for the runner to connect.
            _logger.warning(
                "Host %s reported runner %s exited: %s",
                host_id,
                frame.runner_id,
                frame.error,
            )
            if runner_exit_reports is not None:
                runner_exit_reports.record(frame.runner_id, frame.error, conn.owner)
            if on_runner_exited is not None:
                # Mark the runner's session(s) failed and push the cause
                # to the open view. A runner that crashed before
                # connecting its tunnel has no runner-tunnel disconnect
                # event, so this report is the only failure signal.
                await on_runner_exited(frame.runner_id, frame.error)
            continue

        if isinstance(frame, HostStatResultFrame):
            stat_future = conn.pending_stats.pop(frame.request_id, None)
            if stat_future is not None and not stat_future.done():
                stat_future.set_result(
                    {
                        "status": frame.status,
                        "exists": frame.exists,
                        "type": frame.type,
                        "canonical_path": frame.canonical_path,
                        "error": frame.error,
                    }
                )
            continue

        if isinstance(frame, HostListDirResultFrame):
            list_future = conn.pending_list_dirs.pop(frame.request_id, None)
            if list_future is not None and not list_future.done():
                list_future.set_result(
                    {
                        "status": frame.status,
                        "entries": [
                            {
                                "name": entry.name,
                                "path": entry.path,
                                "type": entry.type,
                                "bytes": entry.bytes,
                                "modified_at": entry.modified_at,
                            }
                            for entry in frame.entries
                        ],
                        "has_more": frame.has_more,
                        "error": frame.error,
                    }
                )
            continue

        if isinstance(frame, HostCreateWorktreeResultFrame):
            create_wt_future = conn.pending_create_worktrees.pop(frame.request_id, None)
            if create_wt_future is not None and not create_wt_future.done():
                create_wt_future.set_result(
                    {
                        "status": frame.status,
                        "worktree_path": frame.worktree_path,
                        "branch": frame.branch,
                        "error": frame.error,
                    }
                )
            continue

        if isinstance(frame, HostRemoveWorktreeResultFrame):
            remove_wt_future = conn.pending_remove_worktrees.pop(frame.request_id, None)
            if remove_wt_future is not None and not remove_wt_future.done():
                remove_wt_future.set_result(
                    {
                        "status": frame.status,
                        "error": frame.error,
                    }
                )
            continue

        if isinstance(frame, HostCreateDirResultFrame):
            create_dir_future = conn.pending_create_dirs.pop(frame.request_id, None)
            if create_dir_future is not None and not create_dir_future.done():
                create_dir_future.set_result(
                    {
                        "status": frame.status,
                        "path": frame.path,
                        "error": frame.error,
                    }
                )
            continue

        _logger.debug(
            "Host %s sent unexpected frame type: %s",
            host_id,
            type(frame).__name__,
        )


async def _ping_loop(
    ws: WebSocket,
    conn: HostConnection,
    host_id: str,
    host_store: HostStore,
) -> None:
    """Send pings every PING_INTERVAL_S; declare dead after misses.

    Each tick that the host is still alive also persists a heartbeat
    (``host_store.heartbeat``) so the host's last-seen timestamp stays
    fresh in the DB. That timestamp is the liveness freshness gate
    (:data:`omnigent.stores.host_store.HOST_LIVENESS_TTL_S`): when a
    host dies without a graceful disconnect, the heartbeat stops, the
    timestamp goes stale, and the host's sessions correctly drop out of
    the connected set even though ``set_offline`` never ran.

    :param ws: Accepted Starlette WebSocket.
    :param conn: Host connection for timing checks.
    :param host_id: Host id for logging.
    :param host_store: Persistent host store the heartbeat is written to.
    """
    while True:
        await asyncio.sleep(PING_INTERVAL_S)
        elapsed = time.time() - conn.last_frame_at
        if elapsed > PING_INTERVAL_S * PING_MISS_THRESHOLD:
            _logger.warning(
                "Host %s missed %d ping intervals (%.0fs); declaring dead",
                host_id,
                PING_MISS_THRESHOLD,
                elapsed,
            )
            with contextlib.suppress(RuntimeError):
                await ws.close(code=4003, reason="ping timeout")
            return
        # The host is still within the liveness window — refresh its
        # last-seen so the freshness gate keeps it in the online set.
        await asyncio.to_thread(host_store.heartbeat, host_id)
        try:
            ping_text = encode_frame(PingFrame(ts=int(time.time() * 1000)))
            conn.outbound_queue.put_nowait(ping_text)
        except Exception:  # noqa: BLE001
            return
