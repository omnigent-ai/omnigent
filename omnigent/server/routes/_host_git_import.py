"""
Server-side proxy for the host clone-and-bundle tunnel frame.

Mirrors ``_host_worktree``: enqueue a ``host.clone_and_bundle`` frame,
register a future on the host connection, and await the result with a
timeout. The host (not the server) runs git. See
designs/SESSION_GIT_WORKTREE.md for the general tunnel pattern.
"""

from __future__ import annotations

import base64
import logging
import secrets
from dataclasses import dataclass

from omnigent.host.frames import HostCloneAndBundleFrame, encode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._host_rpc import await_host_rpc_result

_logger = logging.getLogger(__name__)

# Above the host's own git timeout (120 s) so the host's specific error
# surfaces instead of a generic server-side timeout.
_CLONE_TIMEOUT_S: float = 150.0


class GitImportProxyError(Exception):
    """
    Raised when the host reports a clone-and-bundle operation failure.

    These are typically user-correctable input problems (repository not
    found, bad ref, invalid URL), so the route layer maps this to
    ``INVALID_INPUT`` (400).

    :param message: Human-readable error suitable for the API
        response body, e.g.
        ``"git clone failed: repository not found"``.
    """

    def __init__(self, message: str) -> None:
        """
        Initialize with the user-facing error message.

        :param message: Error string surfaced to the API caller.
        """
        super().__init__(message)
        self.message = message


class GitImportHostUnavailableError(GitImportProxyError):
    """
    Raised when the host can't be reached for a clone-and-bundle operation.

    Connection loss or no reply within the timeout — an infrastructure
    condition, not user input. The route layer maps this to
    ``CONFLICT`` (409). Subclasses :class:`GitImportProxyError` so
    best-effort callers that catch the base type still catch it.
    """


@dataclass
class ClonedBundle:
    """
    Result of a successful host clone-and-bundle operation.

    :param bundle_bytes: Raw ``.tar.gz`` bundle bytes of the cloned
        repository tree (or subpath).
    :param commit_sha: Resolved HEAD commit SHA, e.g.
        ``"a1b2c3d4e5f6..."``.
    :param resolved_ref: The branch or tag actually checked out, e.g.
        ``"main"`` when ``git_ref`` was ``None``.
    """

    bundle_bytes: bytes
    commit_sha: str
    resolved_ref: str


async def clone_and_bundle_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    git_url: str,
    git_ref: str | None,
    git_subpath: str | None,
) -> ClonedBundle:
    """
    Send a ``host.clone_and_bundle`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection to perform the clone on.
    :param git_url: Remote repository URL to clone, e.g.
        ``"https://github.com/owner/repo.git"``.
    :param git_ref: Branch, tag, or SHA to check out, e.g. ``"main"``
        or ``"v1.2.3"``. ``None`` uses the remote's default branch.
    :param git_subpath: Optional subdirectory inside the repo to bundle,
        e.g. ``"packages/core"``. ``None`` bundles the entire repo root.
    :returns: The decoded bundle bytes, resolved commit SHA, and resolved ref.
    :raises GitImportHostUnavailableError: If the host connection drops
        or doesn't respond within :data:`_CLONE_TIMEOUT_S`.
    :raises GitImportProxyError: If the host reports a clone failure or
        returns an incomplete result.
    """
    request_id = secrets.token_hex(8)
    frame = encode_host_frame(
        HostCloneAndBundleFrame(
            request_id=request_id,
            git_url=git_url,
            git_ref=git_ref,
            git_subpath=git_subpath,
        )
    )
    result = await await_host_rpc_result(
        host_registry=host_registry,
        host_conn=host_conn,
        pending=host_conn.pending_clone_bundles,
        request_id=request_id,
        frame=frame,
        op="clone-and-bundle",
        timeout=_CLONE_TIMEOUT_S,
        unavailable_exc=GitImportHostUnavailableError,
        unavailable_hint=("it may be running an older version that does not support git import"),
    )
    if result.get("status") != "ok":
        raise GitImportProxyError(
            f"git clone failed: {result.get('error') or 'host reported no detail'}"
        )
    bundle_b64 = result.get("bundle_b64")
    commit_sha = result.get("commit_sha")
    resolved_ref = result.get("resolved_ref")
    if (
        not isinstance(bundle_b64, str)
        or not isinstance(commit_sha, str)
        or not isinstance(resolved_ref, str)
    ):
        raise GitImportProxyError("host returned an incomplete clone result")
    bundle_bytes = base64.b64decode(bundle_b64)
    return ClonedBundle(
        bundle_bytes=bundle_bytes, commit_sha=commit_sha, resolved_ref=resolved_ref
    )
