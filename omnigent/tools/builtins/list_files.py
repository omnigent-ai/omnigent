"""Built-in tool: list uploaded files."""

from __future__ import annotations

import json
from typing import Any

from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments


def _parse_limit(value: Any) -> tuple[int | None, str | None]:
    """
    Validate and clamp the optional result limit.

    Matches the public contract: default 20, maximum 100, positive integers only.
    """
    if value is None:
        return 20, None
    if not isinstance(value, int) or isinstance(value, bool):
        return None, "'limit' must be an integer"
    if value < 1:
        return None, "'limit' must be at least 1"
    return min(value, 100), None


class ListFilesTool(Tool):
    """
    List files stored in the file store.

    Returns metadata for uploaded files — ID, filename, size,
    and creation timestamp. Supports pagination via ``limit``
    and ``after`` cursor.
    """

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"list_files"``.
        """
        return "list_files"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "List files that have been uploaded or created. "
            "Returns file metadata: ID, filename, size, and "
            "creation time. Use the file_id to download content "
            "with download_file."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: A tool schema dict.
        """
        return {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": (
                    "List files that have been uploaded or created. "
                    "Returns file metadata: ID, filename, size, and "
                    "creation time. Use the file_id to download content "
                    "with download_file."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Maximum number of files to return. Default 20, max 100."
                            ),
                        },
                        "after": {
                            "type": "string",
                            "description": (
                                "Cursor for pagination. Return files after this file ID."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        List files from the file store.

        :param arguments: JSON with optional ``"limit"`` and
            ``"after"`` keys.
        :param ctx: Server-side execution context (unused).
        :returns: JSON string with file list and pagination info.
        """
        args, error = parse_json_object_arguments(arguments)
        if error is not None:
            return json.dumps({"error": error})
        assert args is not None

        limit, error = _parse_limit(args.get("limit"))
        if error is not None:
            return json.dumps({"error": error})
        assert limit is not None

        after = args.get("after")
        if after is not None and not isinstance(after, str):
            return json.dumps({"error": "'after' must be a string"})

        from omnigent.runtime import get_file_store

        file_store = get_file_store()
        if file_store is None:
            return json.dumps({"error": "File store not configured."})

        # Scope to the current session + global (unscoped) files so
        # agents see their own files but not other users'.
        page = file_store.list(
            limit=limit,
            after=after,
            before=None,
            order="desc",
            session_id=ctx.conversation_id,
            include_unscoped=True,
        )

        files = [
            {
                "file_id": f.id,
                "filename": f.filename,
                "bytes": f.bytes,
                "content_type": f.content_type,
                "created_at": f.created_at,
            }
            for f in page.data
        ]

        result: dict[str, Any] = {"files": files}
        if page.has_more:
            result["has_more"] = True
            result["last_id"] = page.last_id
        return json.dumps(result)
