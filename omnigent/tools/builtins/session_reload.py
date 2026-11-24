"""Reload a session's tools by replacing its runner between turns."""

from typing import Any

from omnigent.tools.base import Tool


class SysSessionReloadToolsTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_session_reload_tools"

    @classmethod
    def description(cls) -> str:
        return (
            "Load the current Omnigent tools into this session, or into a session you created, "
            "by restarting its runner once it is idle. The conversation is kept. For your own "
            "session the reload waits until your current turn ends and nothing is queued, so "
            "finish your reply after calling it. Use it when the user asks, or when you need a "
            "tool that was added or changed after this session started."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Session to reload; omit for your own session.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }
