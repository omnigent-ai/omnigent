"""Ordinary messages between independent sessions owned by the same user."""

from typing import Any

from omnigent.server.feature_flags import Feature, resolve_feature_flags
from omnigent.tools.base import Tool


def peer_session_messaging_enabled() -> bool:
    """Whether this process's ``OMNIGENT_FEATURES`` leaves peer messaging enabled."""
    return resolve_feature_flags().enabled(Feature.PEER_SESSION_MESSAGING)


class SysSessionMessageTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_session_message"

    @classmethod
    def description(cls) -> str:
        return (
            "Send a message to another session the user owns. Use it only when the user "
            "explicitly asks, directly or in their instructions. For your own subagents, "
            "use sys_session_send. The receiver sees it as a user message from your "
            "session ID, so make it self-contained. Replies are not forwarded; read them "
            "with sys_session_get_history. `queued` means accepted, not run. Never resend "
            "an uncertain delivery automatically."
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
