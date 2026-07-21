"""Tool for explicitly renaming the current session."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysSessionRenameTool(Tool):
    """Schema-only tool that renames the calling session."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_session_rename"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Rename the current top-level session with a short summary-style title "
            "(3-6 words, action-first). Strip filler and keep the noun plus verb. "
            "Never copy a conversational question or greeting verbatim. "
            "The rename is ignored if the session title changed concurrently."
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
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": (
                                "Short summary-style, action-first session title, for "
                                "example 'Debug authentication timeout'."
                            ),
                            "minLength": 2,
                            "maxLength": 60,
                        }
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
            },
        }
