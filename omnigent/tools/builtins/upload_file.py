"""Upload file built-in tool.

Takes a relative path in the workspace, validates it stays within
bounds, reads the bytes, and uploads to the file store + artifact
store. Returns a ``file_id`` that the client can use to download
the file via the session-scoped file resource content endpoint.
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from omnigent.tools.base import Tool, ToolContext

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
        parsed: dict[str, Any] = json.loads(arguments) if arguments else {}
        rel_path = parsed.get("path", "")
        if not rel_path:
            return "Error: empty path"

        if ctx.workspace is None:
            return "Error: no workspace available"

        try:
            resolved = safe_resolve(rel_path, ctx.workspace)
        except ValueError as exc:
            return f"Error: {exc}"

        if not resolved.is_file():
            return f"Error: file not found: {rel_path}"

        return _upload(resolved, session_id=ctx.conversation_id)


def _upload(resolved: Path, *, session_id: str | None) -> str:
    """
    Read a file and upload it to the file store.

    :param resolved: Absolute path to the file.
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

    filename = resolved.name
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    data = resolved.read_bytes()

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
