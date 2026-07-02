"""Session compaction builtin tool schema."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysCompactTool(Tool):
    """Request compaction for the current session."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_compact"``."""
        return "sys_compact"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description for the LLM."""
        return (
            "Request context compaction for the current session. Use after "
            "dispatching child work or when the conversation contains stale "
            "routing detail. This preserves conversation history and asks the "
            "active harness/server compaction path to summarize; it does not "
            "delete selected messages."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI function-format schema for ``sys_compact``."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
