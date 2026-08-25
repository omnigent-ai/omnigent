"""Headroom retrieval tool for recovering compressed content.

Provides the headroom_retrieve tool that allows agents to recover
original uncompressed content from compressed messages when CCR
(Content-Cache-Retrieval) is enabled.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from omnigent.runtime.headroom_compression import CCRCache
from omnigent.tools.base import Tool, ToolContext

_logger = logging.getLogger(__name__)


class HeadroomRetrieveTool(Tool):
    """Retrieve original uncompressed content from Headroom cache.

    When CCR (Content-Cache-Retrieval) is enabled, compressed messages include
    a retrieval key. Use this tool to recover the full original content.
    """

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"headroom_retrieve"``.
        """
        return "headroom_retrieve"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "Retrieve original uncompressed content from Headroom cache. "
            "Use this when you encounter compressed messages with a retrieval key "
            "and need to see the full details."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: A tool schema dict.
        """
        return {
            "type": "function",
            "function": {
                "name": "headroom_retrieve",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "The retrieval key from the compressed message",
                        }
                    },
                    "required": ["key"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Execute the tool to retrieve cached content.

        :param arguments: JSON with a ``"key"`` field, e.g. ``'{"key": "abc123"}'``.
        :param ctx: Server-side execution context; uses ``ctx.conversation_id``
            for session isolation.
        :returns: JSON string with ``"content"`` (retrieved text) or ``"error"``.
        """
        try:
            args: dict[str, Any] = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return json.dumps({"error": "malformed JSON arguments"})

        key = args.get("key", "")
        if not key:
            return json.dumps({"error": "No retrieval key provided"})

        try:
            # Use conversation_id from context for session isolation
            conversation_id = ctx.conversation_id or "default"
            cache = CCRCache(cache_dir=None, conversation_id=conversation_id)
            content = cache.retrieve(key)

            if content is None:
                return json.dumps({
                    "error": (
                        f"Content not found for key '{key}'. "
                        "It may have been deleted or never cached."
                    )
                })

            _logger.info(
                "Retrieved CCR content for key %s in conversation %s (%d bytes)",
                key, conversation_id, len(content)
            )
            return json.dumps({"content": content})

        except Exception as e:
            _logger.error("Failed to retrieve CCR content for key %s: %s", key, e)
            return json.dumps({"error": f"Retrieval failed: {e}"})
