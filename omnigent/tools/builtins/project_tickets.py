"""Ticket tools share their validation contracts with the project board API."""

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnigent.entities.project_ticket import (
    CommentOnTicket,
    CreateTicket,
    TicketEventKind,
    TicketStatus,
    UpdateTicket,
)
from omnigent.tools.base import Tool


class ListTickets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: TicketStatus | None = None
    after: str | None = Field(default=None, description="Previous page's last_id.")
    limit: int = Field(default=50, ge=1, le=100)


class GetTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: str
    after: int = Field(default=-1, ge=-1, description="Previous events page's last_position.")
    kind: TicketEventKind | None = None
    sessions_after: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class EditTicket(UpdateTicket):
    ticket_id: str


class AddTicketComment(CommentOnTicket):
    ticket_id: str


class LinkSessionTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: str
    session_id: str | None = Field(default=None, description="Omit to link this session.")
    action: Literal["attach", "remove"] = "attach"


class _TicketTool(Tool):
    tool_name: ClassVar[str]
    help_text: ClassVar[str]
    arguments: ClassVar[type[BaseModel]]

    @classmethod
    def name(cls) -> str:
        return cls.tool_name

    @classmethod
    def description(cls) -> str:
        return (
            cls.help_text
            + " Scoped to this session's project; updates automatically notify its orchestrator."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": self.arguments.model_json_schema(),
            },
        }


class SysTicketListTool(_TicketTool):
    tool_name = "sys_ticket_list"
    help_text = "List tickets, oldest first. Use last_id as after while has_more is true."
    arguments = ListTickets


class SysTicketGetTool(_TicketTool):
    tool_name = "sys_ticket_get"
    help_text = (
        "Read a ticket and pages of its attributed activity and linked sessions. "
        "Use kind=output to read PRs and other links."
    )
    arguments = GetTicket


class SysTicketCreateTool(_TicketTool):
    tool_name = "sys_ticket_create"
    help_text = (
        "Create an Open ticket; only title is required. "
        "Tickets persist independently of sessions and cannot be deleted."
    )
    arguments = CreateTicket


class SysTicketUpdateTool(_TicketTool):
    tool_name = "sys_ticket_update"
    help_text = (
        "Edit a ticket; omit unchanged fields. Done requires completion_summary. "
        "Tags replace the whole list: use needs-human plus a comment when a person must act, "
        "and remove that tag when resolved."
    )
    arguments = EditTicket


class SysTicketCommentTool(_TicketTool):
    tool_name = "sys_ticket_comment"
    help_text = (
        "Append a comment or execution summary, optionally with outputs such as "
        "{type: pull_request, url: ...}. Activity cannot be edited or deleted."
    )
    arguments = AddTicketComment


class SysSessionLinkTicketTool(_TicketTool):
    tool_name = "sys_session_link_ticket"
    help_text = (
        "Attach or remove an existing same-project session's association with a ticket. "
        "Does not create or restart sessions."
    )
    arguments = LinkSessionTicket


TICKET_TOOL_CLASSES = (
    SysTicketListTool,
    SysTicketGetTool,
    SysTicketCreateTool,
    SysTicketUpdateTool,
    SysTicketCommentTool,
    SysSessionLinkTicketTool,
)
TICKET_TOOLS = {tool.name(): tool for tool in TICKET_TOOL_CLASSES}
