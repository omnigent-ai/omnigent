"""
Server-side proxy for the local-session import tunnel frames.

The web import picker runs in a browser and the transcripts live on the
host's disk, so the server never reads them directly: it enqueues a
``host.list_local_sessions`` / ``host.load_local_session`` frame,
registers a future on the host connection, and awaits the result.
Mirrors ``_host_worktree``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from omnigent.host.frames import (
    HostListLocalSessionsFrame,
    HostLoadLocalSessionFrame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry

_logger = logging.getLogger(__name__)

# Browsing parses up to twenty full transcripts and a load can move a
# large one across the tunnel, so allow well over the git-op budget.
_LOCAL_SESSION_TIMEOUT_S: float = 60.0


class LocalSessionProxyError(Exception):
    """
    The host reported a local-session operation failure.

    User-correctable conditions (unsupported source, unreadable
    transcript), so the route layer maps this to ``INVALID_INPUT``
    (400).

    :param message: Human-readable error for the API response body.
    """

    def __init__(self, message: str) -> None:
        """
        Initialize with the user-facing error message.

        :param message: Error string surfaced to the API caller.
        """
        super().__init__(message)
        self.message = message


class LocalSessionNotFoundError(LocalSessionProxyError):
    """
    The requested session does not exist on the host.

    The route layer maps this to ``NOT_FOUND`` (404).
    """


class LocalSessionHostUnavailableError(LocalSessionProxyError):
    """
    The host could not be reached for the operation.

    Connection loss or no reply within the timeout — an infrastructure
    condition, not user input. The route layer maps this to
    ``CONFLICT`` (409). Subclasses :class:`LocalSessionProxyError` so
    best-effort callers that catch the base type still catch it; a
    caller distinguishing the two **must** catch this subclass first.
    """


async def _await_result(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    pending: dict[str, asyncio.Future[dict[str, Any]]],
    request_id: str,
    frame: str,
    op: str,
) -> dict[str, Any]:
    """
    Send one frame and await its correlated result.

    Shared plumbing for the list/load proxies: register a future on
    ``pending`` keyed by ``request_id``, enqueue ``frame``, await the
    reply, and clean up on every path.

    :param host_registry: Registry used to enqueue the outbound frame.
    :param host_conn: Live host connection.
    :param pending: The connection's pending-future map for this op
        (``pending_list_local_sessions`` or
        ``pending_load_local_session``).
    :param request_id: Correlation id already embedded in ``frame``.
    :param frame: Encoded outbound frame.
    :param op: Short label for log and error text, e.g.
        ``"list_local_sessions"``.
    :returns: The host's decoded result dict.
    :raises LocalSessionHostUnavailableError: On connection loss or no
        reply within :data:`_LOCAL_SESSION_TIMEOUT_S`.
    """
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    pending[request_id] = future
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError as exc:
            raise LocalSessionHostUnavailableError(
                f"host '{host_conn.host_id}' connection lost during {op}"
            ) from exc
        try:
            return await asyncio.wait_for(future, timeout=_LOCAL_SESSION_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            _logger.warning(
                "host '%s' did not answer %s within %.0fs",
                host_conn.host_id,
                op,
                _LOCAL_SESSION_TIMEOUT_S,
            )
            raise LocalSessionHostUnavailableError(
                f"host '{host_conn.host_id}' did not respond to {op} within "
                f"{_LOCAL_SESSION_TIMEOUT_S:.0f}s (it may be running an older version)"
            ) from exc
    finally:
        pending.pop(request_id, None)


async def list_local_sessions_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    source: str,
    limit: int,
) -> list[dict[str, Any]]:
    """
    Send a ``host.list_local_sessions`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection to list sessions on.
    :param source: Harness that owns the sessions — ``"claude"`` or
        ``"codex"``.
    :param limit: Maximum sessions to return, e.g. ``10``.
    :returns: Session summary dicts, newest first.
    :raises LocalSessionHostUnavailableError: If the host connection
        drops or doesn't respond within :data:`_LOCAL_SESSION_TIMEOUT_S`.
    :raises LocalSessionProxyError: If the host reports a listing
        failure.
    """
    request_id = secrets.token_hex(8)
    frame = encode_host_frame(
        HostListLocalSessionsFrame(request_id=request_id, source=source, limit=limit)
    )
    result = await _await_result(
        host_registry=host_registry,
        host_conn=host_conn,
        pending=host_conn.pending_list_local_sessions,
        request_id=request_id,
        frame=frame,
        op="list_local_sessions",
    )
    if result.get("status") != "ok":
        raise LocalSessionProxyError(
            str(result.get("error") or "the host could not list local sessions")
        )
    sessions = result.get("sessions")
    if not isinstance(sessions, list):
        raise LocalSessionProxyError("the host returned an invalid session list")
    return sessions


async def load_local_session_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    source: str,
    external_session_id: str,
) -> dict[str, Any]:
    """
    Send a ``host.load_local_session`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection to load the session from.
    :param source: Harness that owns the session — ``"claude"`` or
        ``"codex"``.
    :param external_session_id: Harness-native session id.
    :returns: ``{"source", "external_session_id", "workspace", "items"}``.
    :raises LocalSessionNotFoundError: If the host has no such session.
    :raises LocalSessionHostUnavailableError: If the host connection
        drops or doesn't respond within :data:`_LOCAL_SESSION_TIMEOUT_S`.
    :raises LocalSessionProxyError: If the host reports a load failure.
    """
    request_id = secrets.token_hex(8)
    frame = encode_host_frame(
        HostLoadLocalSessionFrame(
            request_id=request_id,
            source=source,
            external_session_id=external_session_id,
        )
    )
    result = await _await_result(
        host_registry=host_registry,
        host_conn=host_conn,
        pending=host_conn.pending_load_local_session,
        request_id=request_id,
        frame=frame,
        op="load_local_session",
    )
    status = result.get("status")
    if status == "not_found":
        raise LocalSessionNotFoundError(
            str(result.get("error") or f"{source} session {external_session_id!r} was not found")
        )
    if status != "ok":
        raise LocalSessionProxyError(
            str(result.get("error") or "the host could not load the session")
        )
    session = result.get("session")
    if not isinstance(session, dict):
        raise LocalSessionProxyError("the host returned an invalid session payload")
    return session
