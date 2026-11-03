"""Upload file built-in tool.

Takes a relative path in the workspace, validates it stays within
bounds, reads the bytes, and uploads to the file store + artifact
store. Returns a ``file_id`` that the client can use to download
the file via the session-scoped file resource content endpoint.
"""

from __future__ import annotations

import json
import mimetypes
import os
import stat
from pathlib import Path
from typing import Any, BinaryIO

from omnigent.inner.os_env import _DIR_TRAVERSE_FLAGS
from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments

_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "upload_file",
        "description": (
            "Upload a file from the workspace so the user can download it. "
            "Takes a relative path within the workspace (e.g. 'output/chart.png'). "
            "Returns a file_id that the user's client can use to download the file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path to the file within the workspace, "
                        "e.g. 'output/chart.png' or 'results.csv'."
                    ),
                },
            },
            "required": ["path"],
        },
    },
}


def safe_resolve(path: str, workspace: Path) -> Path:
    """
    Resolve a relative path against the workspace with traversal protection.

    :param path: Relative path string from the LLM.
    :param workspace: The workspace root directory.
    :returns: The resolved absolute path.
    :raises ValueError: If the path escapes the workspace.
    """
    resolved = (workspace / path).resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise ValueError(f"Path escapes workspace: {path}")
    if resolved.is_symlink():
        target = resolved.resolve(strict=True)
        if not target.is_relative_to(workspace.resolve()):
            raise ValueError(f"Symlink escapes workspace: {path}")
    return resolved


_OPEN_BENEATH_SUPPORTED = (
    os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
)


def safe_open(resolved: Path, workspace: Path) -> BinaryIO:
    """
    Open a path :func:`safe_resolve` accepted, keeping it confined at open time.

    A component swapped for a symlink after the check would redirect a plain
    ``open``, so each component below the workspace root is reopened with
    ``O_NOFOLLOW``. Platforms without ``dir_fd`` support open by path.

    :param resolved: Path returned by :func:`safe_resolve` for ``workspace``.
    :param workspace: The workspace root directory.
    :returns: The file opened for binary reading.
    :raises ValueError: If the path is outside the workspace or is not a
        regular file.
    :raises OSError: If a component is missing or is now a symlink.
    """
    root = workspace.resolve()
    parts = resolved.relative_to(root).parts
    if _OPEN_BENEATH_SUPPORTED and parts:
        fd = _open_beneath(root, parts)
    else:
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"Not a regular file: {resolved.name}")
    return os.fdopen(fd, "rb")


def _open_beneath(root: Path, parts: tuple[str, ...]) -> int:
    """
    Open ``root/parts`` for reading without following a symlink below ``root``.

    :param root: Resolved directory the walk starts from.
    :param parts: Path components below ``root``; the last names the file.
    :returns: A read-only descriptor for the file.
    :raises OSError: If a component is missing, not a directory, or a symlink.
    """
    dir_fd = os.open(root, _DIR_TRAVERSE_FLAGS)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _DIR_TRAVERSE_FLAGS | os.O_NOFOLLOW, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        # O_NONBLOCK keeps a FIFO planted at the leaf from hanging the open.
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


class UploadFileTool(Tool):
    """
    Upload a file from the workspace to the file store.

    The tool reads the file, stores it via the runtime's file store
    and artifact store globals, and returns a JSON result with
    ``file_id``, ``filename``, and ``content_type``.
    """

    @classmethod
    def name(cls) -> str:
        """
        Tool name for dispatch and schema registration.

        :returns: ``"upload_file"``.
        """
        return "upload_file"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "Upload a file from the workspace so the user can download it. "
            "Takes a relative path within the workspace (e.g. 'output/chart.png'). "
            "Returns a file_id that the user's client can use to download the file."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI function schema.

        :returns: The schema dict.
        """
        return _SCHEMA

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Upload a file from the workspace to the file store.

        :param arguments: JSON with ``"path"`` key (relative to workspace).
        :param ctx: Execution context with ``workspace`` path.
        :returns: JSON string with ``file_id``, ``filename``, ``content_type``.
        """
        parsed, error = parse_json_object_arguments(arguments)
        if error is not None:
            return f"Error: {error}"
        assert parsed is not None
        rel_path = parsed.get("path", "")
        if not isinstance(rel_path, str) or not rel_path:
            return "Error: path must be a non-empty string"

        if ctx.workspace is None:
            return "Error: no workspace available"

        try:
            resolved = safe_resolve(rel_path, ctx.workspace)
        except ValueError as exc:
            return f"Error: {exc}"

        if not resolved.is_file():
            return f"Error: file not found: {rel_path}"

        try:
            file = safe_open(resolved, ctx.workspace)
        except (OSError, ValueError) as exc:
            return f"Error: {exc}"
        with file:
            return _upload(file, resolved.name, session_id=ctx.conversation_id)


def _upload(file: BinaryIO, filename: str, *, session_id: str | None) -> str:
    """
    Read a file and upload it to the file store.

    :param file: Open workspace file to read.
    :param filename: Name to store the file under, e.g. ``"chart.png"``.
    :param session_id: Owning conversation/session id,
        e.g. ``"conv_abc123"``.
    :returns: JSON result string with file_id.
    """
    from omnigent.runtime import get_artifact_store, get_file_store

    file_store = get_file_store()
    artifact_store = get_artifact_store()

    if file_store is None or artifact_store is None:
        return "Error: file store not available"
    if session_id is None:
        return "Error: upload_file requires a session id"

    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    data = file.read()

    file_record = file_store.create(
        filename=filename,
        bytes=len(data),
        content_type=content_type,
        session_id=session_id,
    )
    # Key must match the file content endpoint, which reads via
    # artifact_store.get(stored.id).
    artifact_store.put(file_record.id, data)

    return json.dumps(
        {
            "file_id": file_record.id,
            "filename": filename,
            "content_type": content_type,
        }
    )
