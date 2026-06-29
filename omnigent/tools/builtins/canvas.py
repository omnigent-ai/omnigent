"""Built-in ``set_canvas`` tool — author the conversation's canvas artifact.

The agent calls ``set_canvas`` with HTML or Markdown; the content is stored
(one canvas per conversation, overwritten on re-set) and the web UI renders it
in a right-rail Canvas tab. Resolves the store lazily via
:func:`omnigent.runtime.get_canvas_store`, like the other store-backed tools.
"""

from __future__ import annotations

import json
from typing import Any

from omnigent.entities.canvas import CANVAS_CONTENT_TYPES
from omnigent.tools.base import Tool, ToolContext

_CONTENT_TYPES = sorted(CANVAS_CONTENT_TYPES)


class SetCanvasTool(Tool):
    """Create or overwrite the conversation's rendered canvas artifact."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"set_canvas"``."""
        return "set_canvas"

    @classmethod
    def description(cls) -> str:
        """:returns: Description visible to the LLM."""
        return (
            "Render an artifact in the user's Canvas pane: pass HTML (default) "
            "or Markdown and a title. Use this to show a visual result — a "
            "table, chart, diagram, formatted report, or a small interactive "
            "HTML/JS widget. Calling it again overwrites the conversation's "
            "canvas, so you can iterate."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI tool schema for ``set_canvas``."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Canvas title."},
                        "content": {
                            "type": "string",
                            "description": "The artifact body (HTML or Markdown source).",
                        },
                        "content_type": {
                            "type": "string",
                            "enum": _CONTENT_TYPES,
                            "description": "How to render content (default 'html').",
                        },
                        "conversation_id": {
                            "type": "string",
                            "description": "Target conversation (defaults to the current one).",
                        },
                    },
                    "required": ["title", "content"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """Upsert the conversation's canvas; return it as JSON."""
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid arguments: {exc}"})

        title = args.get("title")
        content = args.get("content")
        if not isinstance(title, str) or not title.strip():
            return json.dumps({"error": "title is required"})
        if not isinstance(content, str) or not content:
            return json.dumps({"error": "content is required"})

        content_type = args.get("content_type", "html")
        if content_type not in CANVAS_CONTENT_TYPES:
            return json.dumps({"error": f"content_type must be one of {_CONTENT_TYPES}"})

        conversation_id = args.get("conversation_id") or ctx.conversation_id
        if not conversation_id:
            return json.dumps({"error": "set_canvas requires a conversation context"})

        from omnigent.db.utils import generate_canvas_id
        from omnigent.runtime import get_canvas_store

        store = get_canvas_store()
        if store is None:
            return json.dumps({"error": "canvas store is not configured on this server"})

        canvas = store.upsert(
            generate_canvas_id(),
            conversation_id,
            title.strip(),
            content,
            content_type,
        )
        return json.dumps(
            {
                "ok": True,
                "canvas": {
                    "id": canvas.id,
                    "conversation_id": canvas.conversation_id,
                    "title": canvas.title,
                    "content_type": canvas.content_type,
                    "updated_at": canvas.updated_at,
                },
            }
        )
