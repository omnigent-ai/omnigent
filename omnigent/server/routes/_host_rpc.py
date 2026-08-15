"""
Shared request/reply plumbing for host tunnel RPCs.

The host worktree and git-import proxies both follow the same pattern:
register a per-``request_id`` future on the connection, enqueue an
outbound frame, await the matching reply with a timeout, and clean up on
every path. :func:`await_host_rpc_result` is that shared block; each
caller supplies its own pending-future map, timeout, and
host-unavailable exception type so the two proxies keep their distinct
error hierarchies (worktree vs git-import).
"""

from __future__ import annotations

import asyncio
from typing import Any

from omnigent.server.host_registry import HostConnection, HostRegistry


async def await_host_rpc_result(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    pending: dict[str, asyncio.Future[dict[str, Any]]],
    request_id: str,
    frame: str,
    op: str,
    timeout: float,
    unavailable_exc: type[Exception],
    unavailable_hint: str,
) -> dict[str, Any]:
    """
    Send a host frame and await its matching reply over the tunnel.

    Registers a future on ``pending`` keyed by ``request_id``, enqueues
    ``frame``, awaits the reply, and always removes the future in a
    ``finally`` block.

    :param host_registry: Registry used to enqueue the outbound frame.
    :param host_conn: Live host connection.
    :param pending: The connection's pending-future map for this op
        (e.g. ``pending_create_worktrees`` or ``pending_clone_bundles``).
    :param request_id: Correlation id already embedded in ``frame``.
    :param frame: Encoded host frame to send.
    :param op: Short label for error messages, e.g. ``"worktree creation"``
        or ``"clone-and-bundle"``.
    :param timeout: Seconds to wait for the reply before treating the
        host as unavailable. Read at call time by each proxy from its own
        module-level constant so tests can monkeypatch it.
    :param unavailable_exc: Exception type raised on connection loss or
        timeout (``WorktreeHostUnavailableError`` /
        ``GitImportHostUnavailableError``). Constructed with a single
        message argument.
    :param unavailable_hint: Parenthetical tail appended to the timeout
        message, e.g. ``"it may be running an older version that does not
        support worktrees"``.
    :returns: The host's result dict (``status`` plus op-specific fields).
    :raises Exception: ``unavailable_exc`` on connection loss or no reply
        within ``timeout``.
    """
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    pending[request_id] = future
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError as exc:
            raise unavailable_exc(
                f"host '{host_conn.host_id}' connection lost during {op}"
            ) from exc
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise unavailable_exc(
                f"host '{host_conn.host_id}' did not respond to {op} within "
                f"{timeout:.0f}s ({unavailable_hint})"
            ) from exc
    finally:
        pending.pop(request_id, None)
