"""
Server-side proxy for the host workspace-filesystem tunnel frames.

When a session's runner is offline but the host that holds the workspace
on disk is still connected, the filesystem endpoints fall back to reading
the workspace over the host tunnel instead of returning 502. This module
mirrors ``_host_worktree``: enqueue a ``host.fs_request`` frame, register
a future on the host connection, and await the ``host.fs_result``.

The host runs :class:`omnigent.workspace_fs.WorkspaceReader` and returns
the same JSON the runner's filesystem endpoints would, so the endpoint
layer and the frontend cannot tell which side answered.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from omnigent.host.frames import HostFsRequestFrame, encode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry

_logger = logging.getLogger(__name__)

# The host runs git status / directory walks synchronously; keep this
# above the reader's own git timeout (5s) so a slow read surfaces the
# host's specific error rather than a generic server timeout.
_FS_TIMEOUT_S: float = 20.0


class HostFsError(Exception):
    """A host-served filesystem read failed with a mappable outcome.

    Carries the ``status``/``code``/``message`` the runner would have
    returned so the endpoint layer can reproduce the same HTTP response.

    :param status: HTTP status, e.g. ``404``.
    :param code: Machine-readable error code, e.g. ``"not_found"``.
    :param message: Human-readable detail.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class HostFsUnavailableError(Exception):
    """The host could not be reached for a filesystem read.

    Connection loss or no reply within the timeout — an infrastructure
    condition. Callers treat it like a runner-offline result (fall
    through to the next resolver link / 502).
    """


async def read_workspace_from_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    op: str,
    workspace: str,
    session_id: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Send a ``host.fs_request`` frame and await its result.

    :param host_registry: Registry used to enqueue the outbound frame.
    :param host_conn: Live host connection for the session's host.
    :param op: Operation name — ``"list_or_read"`` / ``"changes"`` /
        ``"diff"`` / ``"search"``.
    :param workspace: Absolute workspace path on the host.
    :param session_id: Session id, forwarded to the change registry.
    :param params: Operation-specific arguments.
    :returns: The runner-shaped result payload on success.
    :raises HostFsError: When the host reports a filesystem failure
        (404/400/500) — reproduces the runner's response.
    :raises HostFsUnavailableError: On connection loss or timeout.
    """
    request_id = secrets.token_hex(8)
    frame = encode_host_frame(
        HostFsRequestFrame(
            request_id=request_id,
            op=op,
            workspace=workspace,
            session_id=session_id,
            params=params,
        )
    )
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    host_conn.pending_fs_requests[request_id] = future
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError as exc:
            raise HostFsUnavailableError(
                f"host '{host_conn.host_id}' connection lost during fs read"
            ) from exc
        try:
            result = await asyncio.wait_for(future, timeout=_FS_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            _logger.warning(
                "host '%s' did not answer fs op %r within %.0fs",
                host_conn.host_id,
                op,
                _FS_TIMEOUT_S,
            )
            raise HostFsUnavailableError(
                f"host '{host_conn.host_id}' did not respond to fs read within "
                f"{_FS_TIMEOUT_S:.0f}s (it may be running an older version)"
            ) from exc
    finally:
        host_conn.pending_fs_requests.pop(request_id, None)

    if result.get("status") == "ok":
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise HostFsUnavailableError(
                f"host '{host_conn.host_id}' returned an incomplete fs result"
            )
        return payload

    # The host reported a filesystem failure — reproduce the runner's
    # HTTP shape from the error fields it sent back.
    status = result.get("error_status")
    code = result.get("error_code") or "fs_read_failed"
    message = result.get("error") or "host filesystem read failed"
    if not isinstance(status, int):
        status = 500
    raise HostFsError(status, str(code), str(message))
