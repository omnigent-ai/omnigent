"""Built-in tool: list review comments for the current session."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from omnigent.tools.base import Tool, ToolContext


class ListCommentsTool(Tool):
    """
    List review comments for the current session.

    Returns open or addressed comments anchored to file ranges,
    so the agent can understand what the user has flagged and
    act on each comment in turn.
    """

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"list_comments"``.
        """
        return "list_comments"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "List review comments for the current session. "
            "Returns comments with the file path, selected text, "
            "character range, and comment body. Use this to see "
            "what the user has flagged before addressing each item."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: A tool schema dict.
        """
        return {
            "type": "function",
            "function": {
                "name": "list_comments",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Filter to comments on a specific file, "
                                'e.g. "src/App.tsx". Omit to return '
                                "comments across all files."
                            ),
                        },
                        "status": {
                            "type": "string",
                            "enum": ["draft", "addressed"],
                            "description": (
                                "Filter by comment status. "
                                '"draft" returns open comments the user '
                                'has not yet addressed; "addressed" returns '
                                "comments already handled. "
                                "Omit to return all statuses."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Return matching comments for the current session.

        :param arguments: JSON with optional ``"path"`` and
            ``"status"`` keys, e.g.
            ``'{"path": "src/App.tsx", "status": "draft"}'``.
        :param ctx: Server-side execution context; uses
            ``ctx.conversation_id`` to scope the query to the
            current session.
        :returns: JSON string with a ``"comments"`` list, each
            entry containing ``id``, ``path``, ``anchor_content``,
            ``start_index``, ``end_index``, ``body``, ``status``,
            ``created_by``, and ``edited_at``.
        """
        if ctx.conversation_id is None:
            return json.dumps({"error": "no conversation context — cannot scope comment query"})

        from omnigent.runtime import get_comment_store

        store = get_comment_store()
        if store is None:
            return json.dumps({"error": "comment store not configured for this deployment"})

        try:
            args: dict[str, Any] = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return json.dumps({"error": "malformed JSON arguments"})
        path: str | None = args.get("path")
        status_filter: str | None = args.get("status")

        comments = store.list_for_conversation(ctx.conversation_id, path=path)
        if status_filter is not None:
            comments = [c for c in comments if c.status == status_filter]

        return json.dumps({"comments": [asdict(c) for c in comments]})
