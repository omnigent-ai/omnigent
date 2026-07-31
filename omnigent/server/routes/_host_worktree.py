"""
Server-side proxies for the host git-worktree tunnel frames.

Like ``_workspace_validation._ask_host_stat``: enqueue a
``host.create_worktree`` / ``host.remove_worktree`` frame, register a
future on the host connection, and await the result with a timeout. The
host (not the server) runs git. See designs/SESSION_GIT_WORKTREE.md.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from omnigent.host.frames import (
    HostCreateWorktreeFrame,
    HostListWorktreesFrame,
    HostRemoveWorktreeFrame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._host_rpc import await_host_rpc_result

_logger = logging.getLogger(__name__)

# Above the host's own git timeout (120 s) so the host's specific error
# surfaces instead of a generic server-side timeout.
_WORKTREE_TIMEOUT_S: float = 150.0


class WorktreeProxyError(Exception):
    """
    Raised when the host reports a worktree operation failure.

    These are typically user-correctable input problems (branch
    already exists, not a git repo, bad base ref), so the route layer
    maps this to ``INVALID_INPUT`` (400).

    :param message: Human-readable error suitable for the API
        response body, e.g.
        ``"worktree creation failed: branch already exists"``.
    """

    def __init__(self, message: str) -> None:
        """
        Initialize with the user-facing error message.

        :param message: Error string surfaced to the API caller.
        """
        super().__init__(message)
        self.message = message


class WorktreeHostUnavailableError(WorktreeProxyError):
    """
    Raised when the host can't be reached for a worktree operation.

    Connection loss or no reply within the timeout — an infrastructure
    condition, not user input. The route layer maps this to
    ``CONFLICT`` (409). Subclasses :class:`WorktreeProxyError` so
    best-effort callers that catch the base type still catch it.
    """


@dataclass
class CreatedWorktree:
    """
    Result of a successful host worktree creation.

    :param worktree_path: Absolute path of the created worktree
        directory on the host, e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``. Stored as
        the session ``workspace``.
    :param branch: The branch checked out in the worktree, e.g.
        ``"feature/login"``.
    """

    worktree_path: str
    branch: str


_WORKTREE_UNAVAILABLE_HINT = "it may be running an older version that does not support worktrees"


async def create_worktree_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    repo_path: str,
    branch_name: str,
    base_branch: str | None,
) -> CreatedWorktree:
    """
    Send a ``host.create_worktree`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection to create the worktree on.
    :param repo_path: Absolute path inside the source repo on the
        host — the canonical picked directory, e.g.
        ``"/Users/alice/myrepo"``.
    :param branch_name: New branch to create, e.g. ``"feature/login"``.
    :param base_branch: Optional base ref, e.g. ``"main"``. ``None``
        branches from the repo's current ``HEAD``.
    :returns: The created worktree's path and branch.
    :raises WorktreeHostUnavailableError: If the host connection drops
        or doesn't respond within :data:`_WORKTREE_TIMEOUT_S`.
    :raises WorktreeProxyError: If the host reports a worktree failure.
    """
    request_id = secrets.token_hex(8)
    frame = encode_host_frame(
        HostCreateWorktreeFrame(
            request_id=request_id,
            repo_path=repo_path,
            branch_name=branch_name,
            base_branch=base_branch,
        )
    )
    result = await await_host_rpc_result(
        host_registry=host_registry,
        host_conn=host_conn,
        pending=host_conn.pending_create_worktrees,
        request_id=request_id,
        frame=frame,
        op="worktree creation",
        timeout=_WORKTREE_TIMEOUT_S,
        unavailable_exc=WorktreeHostUnavailableError,
        unavailable_hint=_WORKTREE_UNAVAILABLE_HINT,
    )
    if result.get("status") != "ok":
        raise WorktreeProxyError(
            f"worktree creation failed: {result.get('error') or 'host reported no detail'}"
        )
    worktree_path = result.get("worktree_path")
    branch = result.get("branch")
    if not isinstance(worktree_path, str) or not isinstance(branch, str):
        raise WorktreeProxyError("host returned an incomplete worktree result")
    return CreatedWorktree(worktree_path=worktree_path, branch=branch)


async def remove_worktree_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    worktree_path: str,
    branch: str | None,
    delete_branch: bool,
) -> None:
    """
    Send a ``host.remove_worktree`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection that owns the worktree.
    :param worktree_path: Absolute path of the worktree to remove on
        the host, e.g. ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Branch to delete when ``delete_branch`` is
        ``True``, e.g. ``"feature/login"``. ``None`` skips branch
        deletion.
    :param delete_branch: When ``True``, delete ``branch`` after
        removing the worktree directory.
    :raises WorktreeHostUnavailableError: If the host connection drops
        or doesn't respond within :data:`_WORKTREE_TIMEOUT_S`.
    :raises WorktreeProxyError: If the host reports a removal failure.
    """
    request_id = secrets.token_hex(8)
    frame = encode_host_frame(
        HostRemoveWorktreeFrame(
            request_id=request_id,
            worktree_path=worktree_path,
            branch=branch,
            delete_branch=delete_branch,
        )
    )
    result = await await_host_rpc_result(
        host_registry=host_registry,
        host_conn=host_conn,
        pending=host_conn.pending_remove_worktrees,
        request_id=request_id,
        frame=frame,
        op="worktree removal",
        timeout=_WORKTREE_TIMEOUT_S,
        unavailable_exc=WorktreeHostUnavailableError,
        unavailable_hint=_WORKTREE_UNAVAILABLE_HINT,
    )
    if result.get("status") != "ok":
        raise WorktreeProxyError(
            f"worktree removal failed: {result.get('error') or 'host reported no detail'}"
        )


async def list_worktrees_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    repo_path: str,
) -> list[dict[str, object]]:
    """
    Send a ``host.list_worktrees`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection to list worktrees on.
    :param repo_path: Absolute path inside the source repo on the
        host — the canonical picked directory, e.g.
        ``"/Users/alice/myrepo"``.
    :returns: One dict per worktree with keys ``path``, ``branch``,
        ``is_main``, ``detached`` (main first).
    :raises WorktreeHostUnavailableError: If the host connection drops
        or doesn't respond within :data:`_WORKTREE_TIMEOUT_S`.
    :raises WorktreeProxyError: If the host reports a listing failure.
    """
    request_id = secrets.token_hex(8)
    frame = encode_host_frame(
        HostListWorktreesFrame(
            request_id=request_id,
            repo_path=repo_path,
        )
    )
    result = await await_host_rpc_result(
        host_registry=host_registry,
        host_conn=host_conn,
        pending=host_conn.pending_list_worktrees,
        request_id=request_id,
        frame=frame,
        op="worktree listing",
        timeout=_WORKTREE_TIMEOUT_S,
        unavailable_exc=WorktreeHostUnavailableError,
        unavailable_hint=_WORKTREE_UNAVAILABLE_HINT,
    )
    if result.get("status") != "ok":
        raise WorktreeProxyError(
            f"worktree listing failed: {result.get('error') or 'host reported no detail'}"
        )
    worktrees = result.get("worktrees")
    if not isinstance(worktrees, list):
        raise WorktreeProxyError("host returned an incomplete worktree list")
    return worktrees
