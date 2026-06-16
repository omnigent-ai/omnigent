"""Built-in tool: download a file to the workspace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omnigent.tools.base import Tool, ToolContext


class DownloadFileTool(Tool):
    """
    Download a file from the file store to the workspace.

    Retrieves the binary content by file ID from the artifact
    store and writes it to the agent's workspace directory.
    Returns the local path so the agent can read or process it.
    """

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"download_file"``.
        """
        return "download_file"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "Download a file by its file_id to the workspace. "
            "Returns the local file path. Use list_files to "
            "find available file IDs."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: A tool schema dict.
        """
        return {
            "type": "function",
            "function": {
                "name": "download_file",
                "description": (
                    "Download a file by its file_id to the workspace. "
                    "Returns the local file path. Use list_files to "
                    "find available file IDs."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_id": {
                            "type": "string",
                            "description": ('The file ID to download, e.g. "file_abc123".'),
                        },
                    },
                    "required": ["file_id"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Download a file and save it to the workspace.

        :param ctx: Provides workspace path for saving.
        :returns: JSON string with the local file path, or error.
        """
        args: dict[str, Any] = json.loads(arguments)
        file_id = args.get("file_id")
        if not file_id:
            return json.dumps({"error": "missing required 'file_id'"})

        from omnigent.runtime import get_artifact_store, get_file_store

        file_store = get_file_store()
        artifact_store = get_artifact_store()
        if file_store is None or artifact_store is None:
            return json.dumps({"error": "File store not configured."})

        record = file_store.get(file_id)
        if record is None:
            return json.dumps({"error": f"File {file_id!r} not found."})

        # Reject session-scoped files that belong to a different
        # conversation (ownership, not just existence).
        if record.session_id is not None and record.session_id != ctx.conversation_id:
            return json.dumps({"error": f"File {file_id!r} not found."})

        try:
            data = artifact_store.get(file_id)
        except KeyError:
            return json.dumps(
                {
                    "error": f"File content for {file_id!r} not found.",
                }
            )

        dest = _resolve_destination(
            record.filename,
            ctx.workspace,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

        return json.dumps(
            {
                "path": str(dest),
                "filename": record.filename,
                "bytes": len(data),
                "content_type": record.content_type,
            }
        )


def _resolve_destination(
    filename: str,
    workspace: Path | None,
) -> Path:
    """
    Resolve the save path for a downloaded file.

    :param filename: The file's original filename from the store.
    :param workspace: The agent's workspace directory, or ``None``.
    :returns: Absolute path to save the file.
    """
    base = workspace or Path.cwd()
    return base / filename
