"""Built-in tool: search past conversations for relevant information."""

from __future__ import annotations

import json
from typing import Any

from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments

_MAX_SEARCH_RESULTS = 100


class SearchConversationsTool(Tool):
    """
    Full-text search across all conversations.

    Uses the FTS index to find past messages, tool calls, and
    results that match a query. Returns matching items with
    surrounding context so the agent can recall information
    from prior interactions.
    """

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"search_conversations"``.
        """
        return "search_conversations"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "Search past conversations for relevant information. "
            "Use this to recall details from prior interactions — "
            "e.g. decisions made, code reviewed, files created, "
            "or facts discussed. Returns matching messages ranked "
            "by relevance with surrounding context."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: A tool schema dict.
        """
        return {
            "type": "function",
            "function": {
                "name": "search_conversations",
                "description": (
                    "Search past conversations for relevant information. "
                    "Use this to recall details from prior interactions — "
                    "e.g. decisions made, code reviewed, files created, "
                    "or facts discussed. Returns matching messages ranked "
                    "by relevance with surrounding context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "The search query. Use keywords or "
                                "phrases from the information you're "
                                "looking for."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": ("Maximum number of results to return. Default 10."),
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Search conversations and return matching items with context.

        :param arguments: JSON with ``"query"`` and optional
            ``"limit"`` keys, e.g.
            ``'{"query": "database config", "limit": 5}'``.
        :param ctx: Server-side execution context (unused).
        :returns: JSON string with search results.
        """
        args, error = parse_json_object_arguments(arguments)
        if error is not None:
            return json.dumps({"error": error})
        assert args is not None
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return json.dumps({"error": "missing required 'query' argument"})
        query = query.strip()
        limit = args.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return json.dumps({"error": "limit must be a positive integer"})
        limit = min(limit, _MAX_SEARCH_RESULTS)

        from omnigent.runtime import get_conversation_store

        conv_store = get_conversation_store()
        items = conv_store.search(query, limit=limit)

        if not items:
            return json.dumps({"results": [], "message": "No matching conversations found."})

        results = _format_results(items)
        return json.dumps({"results": results})


def _format_results(
    items: list[Any],
) -> list[dict[str, Any]]:
    """
    Format conversation items into search result dicts.

    Each result includes the item type, conversation ID,
    timestamp, and extracted text content.

    :param items: ConversationItem objects from the store search.
    :returns: List of result dicts with ``conversation_id``,
        ``created_at``, ``type``, ``role``, and ``text`` fields.
    """
    results: list[dict[str, Any]] = []
    for item in items:
        result: dict[str, Any] = {
            "conversation_id": item.response_id,
            "item_id": item.id,
            "created_at": item.created_at,
            "type": item.type,
        }
        text = _extract_text(item)
        if text:
            result["text"] = text
        if hasattr(item.data, "role"):
            result["role"] = item.data.role
        results.append(result)
    return results


def _extract_text(item: Any) -> str:
    """
    Extract readable text from a conversation item.

    :param item: A ConversationItem.
    :returns: The extracted text, or empty string.
    """
    from omnigent.entities import (
        FunctionCallData,
        FunctionCallOutputData,
        MessageData,
    )

    if isinstance(item.data, MessageData):
        parts = []
        for block in item.data.content:
            if isinstance(block, dict):
                text = block.get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(item.data, FunctionCallData):
        return f"[tool call: {item.data.name}({item.data.arguments})]"
    if isinstance(item.data, FunctionCallOutputData):
        return item.data.output
    return ""
