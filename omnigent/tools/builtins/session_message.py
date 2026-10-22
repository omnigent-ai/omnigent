"""Independent-session messaging, separate from child lifecycle operations."""

from typing import Any

from omnigent.tools.base import Tool


class SysSessionMessageTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_session_message"

    @classmethod
    def description(cls) -> str:
        return (
            "Send an ordinary message to another existing session. Requires the original "
            "requester to own both sessions and enable session communication. The target "
            "uses its normal queue/steering behavior. Only an explicit send back returns "
            "a reply; other output stays in the target. This does not create or reparent "
            "sessions. Do not automatically retry an uncertain delivery."
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
                        "session_id": {"type": "string", "description": "Destination session ID."},
                        "message": {"type": "string", "minLength": 1, "maxLength": 8000},
                    },
                    "required": ["session_id", "message"],
                    "additionalProperties": False,
                },
            },
        }


MESSAGE_TOOL_TYPES = (SysSessionMessageTool,)
MESSAGE_TOOL_NAMES = frozenset(tool.name() for tool in MESSAGE_TOOL_TYPES)
