"""Helpers for paths that are interpreted on a remote host."""

from __future__ import annotations

import re

_WINDOWS_DRIVE_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[/\\]")


def is_windows_absolute_path(path: str) -> bool:
    """Return whether *path* is an absolute Windows path string.

    :param path: Host path as supplied by a client or returned by a host.
    :returns: ``True`` for drive-absolute paths (``C:/x`` or ``C:\\x``)
        and UNC paths (``\\\\server\\share``).
    """
    return bool(_WINDOWS_DRIVE_ABSOLUTE_RE.match(path)) or path.startswith("\\\\")


def is_host_absolute_path(path: str) -> bool:
    """Return whether *path* is absolute on POSIX or Windows hosts."""
    return path.startswith("/") or is_windows_absolute_path(path)


def normalize_host_filesystem_route_path(path: str) -> str:
    """Normalize a FastAPI ``:path`` capture for host filesystem browsing.

    FastAPI strips the leading POSIX slash from ``/filesystem/{path:path}``,
    so existing POSIX callers send ``Users/me`` and the route forwards
    ``/Users/me``. Windows drive-absolute paths, however, arrive as
    ``C:/Users/me`` or ``C:\\Users\\me`` and must not be prefixed with ``/``.

    :param path: Captured route path after URL decoding.
    :returns: Path string to forward to the host.
    """
    if path.startswith("~"):
        return path
    if is_windows_absolute_path(path):
        return path
    if path.startswith("/"):
        return path
    return "/" + path


def is_host_subpath(canonical_workspace: str, canonical_boundary: str) -> bool:
    """Return whether one canonical host path is equal to or under another.

    The host returns canonical paths, but the server may be running on a
    different OS than the host. Compare with both separator families and make
    Windows drive paths case-insensitive while preserving POSIX behavior.
    """
    workspace = canonical_workspace
    boundary = canonical_boundary
    if is_windows_absolute_path(workspace) or is_windows_absolute_path(boundary):
        workspace = workspace.casefold()
        boundary = boundary.casefold()

    if workspace == boundary:
        return True

    trimmed = boundary.rstrip("/\\")
    if not trimmed:
        return workspace.startswith("/")
    return workspace.startswith((trimmed + "/", trimmed + "\\"))
