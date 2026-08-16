"""Tool for archiving the current session when its work is done."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysSessionArchiveTool(Tool):
    """Schema-only tool that archives the calling session."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_session_archive"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Archive the current session: hide it from the default session list and "
            "shut its runner down once this turn ends. Call it as the last action of "
            "a top-level unattended run (a scheduled task or similar automation) that "
            "has finished its work, so the run does not leave an open session behind. "
            "Do not call it while a person is still working in this session. "
            "Nothing is deleted — the full transcript stays readable and the user can "
            "unarchive the session at any time. Takes no arguments; it always targets "
            "the session you are running in."
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
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
