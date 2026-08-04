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
            "Rename the current top-level session. Default to a short "
            "summary-style title (3-6 words, action-first): strip filler, keep "
            "the noun plus verb, and never copy a conversational question or "
            "greeting verbatim. When the user asks for a structured title "
            "instead - for example 'repo::branch::date::role' - write it "
            "exactly as asked; structured titles are supported up to 120 "
            "characters. Rename again whenever the session's subject moves on. "
            "The rename is ignored once the user has renamed the session by "
            "hand, and if the title changed concurrently."
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
                                "Session title. Either a short summary-style, "
                                "action-first phrase such as 'Debug authentication "
                                "timeout', or a structured title the user asked for "
                                "such as "
                                "'my-repo::feat/some-branch::2026-08-04::supervisor'."
                            ),
                            "minLength": 2,
                            "maxLength": 120,
                        }
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
            },
        }
