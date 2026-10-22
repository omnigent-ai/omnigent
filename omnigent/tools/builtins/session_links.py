"""Schemas for owner-authorized session communication."""

from typing import Any, ClassVar

from omnigent.tools.base import Tool, ToolContext


class _LinkedTool(Tool):
    tool_name: ClassVar[str]
    help_text: ClassVar[str]
    properties: ClassVar[dict[str, Any]]
    required: ClassVar[list[str]] = []

    @classmethod
    def name(cls) -> str:
        return cls.tool_name

    @classmethod
    def description(cls) -> str:
        return cls.help_text

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": self.required,
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        return '{"error":"Session links require an authenticated runner and consumed user input"}'


_PAGE = {
    "after": {"type": "string", "description": "Continuation cursor from a previous page"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
}
_MESSAGE = {"type": "string", "minLength": 1, "maxLength": 8000}
_IDENTIFIER = {"type": "string", "minLength": 1}


class SessionLinksTool(_LinkedTool):
    tool_name = "sys_session_links"
    help_text = (
        "List communication activity visible to the requester. "
        "Use sys_session_list to discover sessions. "
        "Activity is not an access grant. "
        "Session links must be enabled on the server."
    )
    properties = {**_PAGE, "incoming": {"type": "boolean"}}


class SessionDelegateTool(_LinkedTool):
    tool_name = "sys_session_delegate"
    help_text = (
        "Send normal input to a worker while the requester enabled session communication, "
        "requiring the requester to own both sessions. The worker keeps its context and normal "
        "steering behavior. Returns a durable request ID; an uncertain delivery must not be "
        "repeated as new work."
    )
    properties = {"target_id": _IDENTIFIER, "message": _MESSAGE}
    required = ["target_id", "message"]


class SessionTaskTool(_LinkedTool):
    tool_name = "sys_session_task"
    help_text = (
        "Poll a linked request's input delivery, worker status, and explicitly addressed replies. "
        "A completed worker turn is not necessarily a reply. Each poll rechecks the original "
        "requester's current ownership and switch."
    )
    properties = {"request_id": _IDENTIFIER, **_PAGE}
    required = ["request_id"]


class SessionReplyTool(_LinkedTool):
    tool_name = "sys_session_reply"
    help_text = (
        "Explicitly reply to a linked request delivered to this session. The server resolves "
        "its requesting manager. The reply is copied into that manager's conversation for "
        "everyone who can read it, now or later. New reverse requests independently check "
        "the requester’s ownership and communication setting."
    )
    properties = {"request_id": _IDENTIFIER, "message": _MESSAGE}
    required = ["request_id", "message"]


LINK_TOOL_TYPES = (SessionLinksTool, SessionDelegateTool, SessionTaskTool, SessionReplyTool)
LINK_TOOL_NAMES = frozenset(tool.name() for tool in LINK_TOOL_TYPES)
