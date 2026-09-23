"""
Workspace validation for ``POST /v1/sessions``.

Implements the seven-step validation described in
``designs/SESSION_WORKSPACE_SELECTION.md`` "Server-side validation
at session create":

0. Host is online.
1. Read agent's ``os_env.cwd`` for boundary computation.
2. Compute boundary path (or no boundary for relative cwd).
3. Stat boundary on the host; reject if missing.
4. Stat workspace on the host; reject if missing. Take its
   canonical_path as the canonical workspace.
5. Validate canonical workspace falls inside canonical boundary.
6. For ``cwd: ./subdir``, stat ``<workspace>/subdir`` and reject
   if missing.
7. Return the canonical workspace string for storage.

The host (not the server) is the source of truth for ``~``
expansion and symlink resolution — this module asks the host via
``host.stat`` frames and stores whatever ``canonical_path`` it
returns.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Any

from omnigent.host.frames import HostStatFrame, encode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry

_logger = logging.getLogger(__name__)

# Treat these spec cwd values as "relative" — the agent doesn't pin
# a specific directory and the workspace is unconstrained.
_RELATIVE_CWD_PLACEHOLDERS: frozenset[str] = frozenset({"", ".", "./"})

# Matches a Windows drive-letter absolute path such as ``C:\`` or ``C:/``.
_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[/\\]")


def _is_windows_absolute_path(path: str) -> bool:
    """Return True for Windows drive-letter absolute paths (``C:\\...`` / ``C:/...``)."""
    return bool(_WINDOWS_ABS_PATH_RE.match(path))


def _is_unc_path(path: str) -> bool:
    """Return True for UNC paths (backslash-share or ``//server/share``)."""
    return path.startswith(("\\\\", "//"))


def restore_host_filesystem_url_path(path: str) -> str:
    """Re-add the leading slash FastAPI's ``:path`` converter strips.

    FastAPI captures ``/v1/hosts/{id}/filesystem/{path:path}`` without
    the URL's leading slash, so POSIX ``/Users/me/proj`` arrives as
    ``Users/me/proj`` and must be restored. Windows drive-letter and
    UNC paths are already absolute in the capture (``C:/Users/me``).
    Prefixing ``/`` would produce ``/C:/Users/me``, which is not a
    directory on the host and is why the workspace picker falls
    through to the drive root.

    Tilde-prefixed paths (``~/foo``) are forwarded unchanged; the
    host expands ``~``.

    :param path: Path captured from the URL, e.g. ``Users/me``,
        ``C:/Users/me/work``, or ``~/projects``.
    :returns: Path to forward to ``host.list_dir``.
    """
    if path.startswith("~") or _is_windows_absolute_path(path) or _is_unc_path(path):
        return path
    if not path.startswith("/"):
        return "/" + path
    return path


# How long to wait for a host.stat round-trip before giving up. Stat
# is a single syscall on the host side and a single WS round trip;
# 5 s is generous for transient network slowness without making
# session-create feel hung if the host is wedged.
_STAT_TIMEOUT_S: float = 5.0


class WorkspaceValidationError(Exception):
    """
    Raised when a workspace pick fails one of the validation steps.

    The route layer maps this to a 400 with the contained message.

    :param message: Human-readable error suitable for the API
        response body, e.g. ``"workspace '/tmp/x' is outside the
        agent's required path '/Users/corey/foo'"``.
    """

    def __init__(self, message: str) -> None:
        """
        Initialize with the user-facing error message.

        :param message: Error string surfaced to the API caller.
        """
        super().__init__(message)
        self.message = message


async def _ask_host_stat(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    path: str,
) -> dict[str, Any]:
    """
    Send a ``host.stat`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection.
    :param path: Absolute or tilde-prefixed path, e.g.
        ``"/Users/corey/universe"`` or ``"~/foo"``. The host
        expands ``~`` itself — the server never does.
    :returns: Dict with the stat result fields:
        ``status`` (``"ok"`` or ``"failed"``), ``exists`` (bool),
        ``type`` (``"directory"``, ``"file"``, ``"other"``, or
        ``None``), ``canonical_path`` (resolved absolute path or
        ``None``), and ``error`` (string or ``None``).
    :raises WorkspaceValidationError: When the host doesn't reply
        within the timeout, when the connection drops, or when the
        host returns a ``status: "failed"`` result with an error
        message (e.g. unexpected I/O error). The latter is mapped
        rather than re-raised so the route's generic exception
        handler doesn't swallow filesystem-error context.
    """
    request_id = secrets.token_hex(8)
    loop = asyncio.get_event_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    host_conn.pending_stats[request_id] = future

    frame = encode_host_frame(HostStatFrame(request_id=request_id, path=path))
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError as exc:
            raise WorkspaceValidationError(
                f"host '{host_conn.host_id}' connection lost during stat"
            ) from exc

        try:
            result = await asyncio.wait_for(future, timeout=_STAT_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise WorkspaceValidationError(
                f"host '{host_conn.host_id}' did not respond to stat within {_STAT_TIMEOUT_S:.0f}s"
            ) from exc
    finally:
        # Clean up the pending entry on every path. The receive
        # loop in host_tunnel.py also pops on success, so
        # pop(..., None) is a no-op when both fire — but if the
        # caller is cancelled mid-await, this is the only
        # cleanup path that runs.
        host_conn.pending_stats.pop(request_id, None)

    if result.get("status") == "failed":
        raise WorkspaceValidationError(
            f"host stat failed for path {path!r}: {result.get('error') or 'unknown error'}"
        )
    return result


def _is_relative_cwd(spec_cwd: str | None) -> bool:
    """
    Return ``True`` for spec cwd values that don't pin a directory.

    Relative cwds (``"."`` / ``"./"`` / ``""`` / ``None``) impose
    no boundary on the workspace pick — the user can pick any
    directory the host exposes.

    :param spec_cwd: Value of ``agent_spec.os_env.cwd`` as written
        in the YAML.
    :returns: ``True`` when the cwd places no boundary.
    """
    if spec_cwd is None:
        return True
    if spec_cwd in _RELATIVE_CWD_PLACEHOLDERS:
        return True
    if spec_cwd.startswith("./"):
        return True
    return False


def _is_subpath_of(canonical_workspace: str, canonical_boundary: str) -> bool:
    """
    Return ``True`` when ``canonical_workspace`` equals the
    boundary or is a subdirectory of it.

    Pure string comparison on canonicalized absolute paths — both
    inputs are realpaths returned by ``host.stat``, so symlinks
    are already resolved and ``..`` segments are gone. Without the
    canonicalization step, this comparison would be unsafe.

    :param canonical_workspace: Realpath returned by host.stat for
        the workspace, e.g. ``"/Users/corey/universe/src/foo"``.
    :param canonical_boundary: Realpath for the agent's boundary,
        e.g. ``"/Users/corey/universe"``.
    :returns: ``True`` when the workspace is the boundary or
        nested under it.
    """
    # host.stat realpath on Windows uses backslashes. Only then treat
    # ``\`` as a separator and ignore drive-letter case. On POSIX a
    # backslash is a legal filename character, so ``/allowed\escape``
    # must not look like a child of ``/allowed``.
    if _is_windows_absolute_path(canonical_workspace) or _is_windows_absolute_path(
        canonical_boundary
    ):
        workspace = canonical_workspace.replace("\\", "/").lower()
        boundary = canonical_boundary.replace("\\", "/").lower()
    else:
        workspace = canonical_workspace
        boundary = canonical_boundary
    if workspace == boundary:
        return True
    # Add a trailing separator so ``/a/foo`` is not treated as a
    # subpath of ``/a/fo`` (prefix collision).
    boundary_with_sep = boundary if boundary.endswith("/") else boundary + "/"
    return workspace.startswith(boundary_with_sep)


async def validate_workspace(
    *,
    host_registry: HostRegistry,
    host_id: str,
    workspace: str,
    spec_cwd: str | None,
    host_name_for_errors: str | None = None,
) -> str:
    """
    Run all session-create validation steps and return the
    canonical workspace path.

    See ``designs/SESSION_WORKSPACE_SELECTION.md`` "Server-side
    validation at session create" for the full step list and the
    reasoning for each error path.

    :param host_registry: Server-side host registry; used to
        find the live connection and send stat frames.
    :param host_id: Target host's stable id, e.g.
        ``"host_a1b2c3d4..."``.
    :param workspace: User-supplied absolute path on the host, e.g.
        ``"/Users/corey/universe/src/foo"``. Tilde-prefixed and
        relative paths are rejected upstream by the request schema.
    :param spec_cwd: Value of the bound agent's ``os_env.cwd``
        from its YAML (or ``None`` when the agent has no os_env
        block). Drives boundary computation.
    :param host_name_for_errors: Optional human-readable host
        name to interpolate into error messages, e.g.
        ``"corey-laptop"``. ``None`` falls back to ``host_id``.
    :returns: The canonical workspace path that should be stored
        on the session row, e.g.
        ``"/Users/corey/universe/src/foo"`` (already realpath).
    :raises WorkspaceValidationError: On any validation failure.
        The exception message is suitable for surfacing to the
        API caller verbatim.
    """
    if not workspace.startswith("/") and not _is_windows_absolute_path(workspace):
        # Belt-and-suspenders. The Pydantic schema layer also
        # rejects this; pin it here so direct callers (tests,
        # other server-internal paths) can't bypass.
        raise WorkspaceValidationError("workspace must be an absolute path starting with /")

    display_host = host_name_for_errors or host_id

    # Step 0: host must be online.
    host_conn = host_registry.get(host_id)
    if host_conn is None:
        raise WorkspaceValidationError(
            f"host '{display_host}' is offline; reconnect the host and try again"
        )

    # Step 4: stat the workspace. Done before the boundary check so
    # a missing workspace surfaces directly (more useful error than
    # "workspace is outside boundary that doesn't exist").
    workspace_stat = await _ask_host_stat(
        host_registry=host_registry,
        host_conn=host_conn,
        path=workspace,
    )
    if not workspace_stat.get("exists"):
        raise WorkspaceValidationError(
            f"workspace path does not exist on host '{display_host}': {workspace}"
        )
    if workspace_stat.get("type") != "directory":
        raise WorkspaceValidationError(
            f"workspace path is not a directory on host '{display_host}': {workspace}"
        )
    canonical_workspace = workspace_stat.get("canonical_path")
    if not isinstance(canonical_workspace, str):
        raise WorkspaceValidationError("host returned an empty canonical_path for the workspace")

    # Steps 2, 3, 5: boundary computation. Skipped when the agent's
    # cwd is relative — relative cwds impose no boundary on the
    # workspace pick.
    if not _is_relative_cwd(spec_cwd):
        # spec_cwd is absolute (with-or-without ``~``); ask the
        # host to canonicalize it so boundary comparisons operate
        # on realpaths (symlinks in either side resolved away).
        boundary_stat = await _ask_host_stat(
            host_registry=host_registry,
            host_conn=host_conn,
            path=spec_cwd or "",  # _is_relative_cwd handled None above
        )
        if not boundary_stat.get("exists"):
            raise WorkspaceValidationError(
                f"agent requires path '{spec_cwd}' which does not exist on host '{display_host}'"
            )
        if boundary_stat.get("type") != "directory":
            raise WorkspaceValidationError(
                f"agent's required path '{spec_cwd}' is not a directory on host '{display_host}'"
            )
        canonical_boundary = boundary_stat.get("canonical_path")
        if not isinstance(canonical_boundary, str):
            raise WorkspaceValidationError(
                "host returned an empty canonical_path for the agent's boundary"
            )

        if not _is_subpath_of(canonical_workspace, canonical_boundary):
            raise WorkspaceValidationError(
                f"workspace '{workspace}' is outside the agent's required path '{spec_cwd}'"
            )

    # Step 6: ``cwd: ./subdir`` requires the named subdir under the
    # picked workspace. Other relative cwds (``"."`` / ``""`` /
    # ``None``) impose no extra check.
    if (
        spec_cwd is not None
        and spec_cwd.startswith("./")
        and spec_cwd not in _RELATIVE_CWD_PLACEHOLDERS
    ):
        # Strip the leading ``./`` to get the bare subdir name.
        subdir = spec_cwd[2:]
        # Build under the canonical workspace so any symlinks in the
        # picked path are already resolved — without that, a user
        # whose workspace is a symlink could fool the existence check.
        subdir_path = canonical_workspace.rstrip("/") + "/" + subdir
        subdir_stat = await _ask_host_stat(
            host_registry=host_registry,
            host_conn=host_conn,
            path=subdir_path,
        )
        if not subdir_stat.get("exists"):
            raise WorkspaceValidationError(
                f"agent expects subdirectory '{subdir}' which is not present at {workspace}"
            )

    return canonical_workspace
