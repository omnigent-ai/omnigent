"""Answer a review question in its comment thread."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from omnigent.tools.base import Tool, ToolContext


class ReplyToCommentTool(Tool):
    """Append an agent-authored reply in the current session."""

    @classmethod
    def name(cls) -> str:
        """Return the reserved built-in name."""
        return "reply_to_comment"

    @classmethod
    def description(cls) -> str:
        """Describe in-thread answers separately from file edits."""
        return "Answer a question in the comment's thread without editing the file."

    def get_schema(self) -> dict[str, Any]:
        """Return the function schema shared by in-process and native tools."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "comment_id": {
                            "type": "string",
                            "description": "Comment ID from list_comments.",
                        },
                        "body": {
                            "type": "string",
                            "description": "Reply text, up to 12,000 UTF-8 bytes.",
                        },
                    },
                    "required": ["comment_id", "body"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """Scope the append to the tool context, never a caller-supplied session."""
        if ctx.conversation_id is None:
            return json.dumps({"error": "no conversation context — cannot scope comment reply"})
        from omnigent.runtime import get_comment_store

        store = get_comment_store()
        if store is None:
            return json.dumps({"error": "comment store not configured for this deployment"})
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError:
            return json.dumps({"error": "malformed JSON arguments"})
        if not isinstance(args, dict):
            return json.dumps({"error": "arguments must be an object"})
        comment_id, body = args.get("comment_id"), args.get("body")
        if not isinstance(comment_id, str) or not comment_id:
            return json.dumps({"error": "missing required argument: comment_id"})
        if not isinstance(body, str):
            return json.dumps({"error": "missing required argument: body"})
        try:
            reply = store.add_reply(comment_id, ctx.conversation_id, body, author_kind="agent")
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        if reply is None:
            return json.dumps({"error": f"comment not found: {comment_id}"})
        return json.dumps({"reply": asdict(reply)})
