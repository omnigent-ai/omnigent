"""Tool for explicitly renaming the current session."""

from __future__ import annotations

from typing import Any

from omnigent.entities import DEFAULT_GENERATED_TITLE_MAX_CHARS
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
            "Propose a concise title for the current top-level session. Include known "
            "PR or issue numbers when relevant. The server applies the user's configured "
            "title requirements, including date prefixes and formatting. Without custom "
            "requirements, use 3-6 words, action-first. Strip filler. "
            "Never copy a conversational question or greeting verbatim. "
            "The rename is silent and can be updated again as the work evolves. "
            "Sub-agent sessions cannot rename themselves (returns not_top_level)."
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
                                "Concise title proposal including relevant PR or issue numbers, "
                                "for example 'Fix PR 123 authentication timeout'. The server "
                                "may reformat it to match the user's title requirements."
                            ),
                            "minLength": 2,
                            "maxLength": DEFAULT_GENERATED_TITLE_MAX_CHARS,
                        }
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
            },
        }
