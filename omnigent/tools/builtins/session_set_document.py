"""Tool for linking a Google Doc to the calling session."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysSessionSetDocumentTool(Tool):
    """Schema-only tool for setting the current session's linked document."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_session_set_document"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Attach an existing Google Doc to the current session. Pass its HTTPS Google Docs "
            "editor URL, optionally with a tab. Heading, bookmark, comment, and access-key links "
            "are unsupported. On success the tool returns the saved canonical URL."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        }
