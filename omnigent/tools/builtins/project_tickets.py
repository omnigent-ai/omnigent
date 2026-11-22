"""Ticket tools share their validation contracts with the project board API."""

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities.project_ticket import (
    CommentOnTicket,
    CreateTicket,
    LinkRelatedTicket,
    TicketEventKind,
    TicketSessionLinkOptions,
    TicketStageDefinition,
    TicketStageId,
    UpdateTicket,
    UpdateTicketStages,
)
from omnigent.tools.base import Tool


class ListTickets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage_id: TicketStageId | None = Field(
        default=None, description="Filter by a stage ID from this project's definitions."
    )
    after: str | None = Field(default=None, description="Previous page's last_id.")
    limit: int = Field(default=50, ge=1, le=100)


class GetTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: str
    after: int = Field(default=-1, ge=-1, description="Previous events page's last_position.")
    kind: TicketEventKind | None = None
    sessions_after: str | None = None
    related_after: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class EditTicket(UpdateTicket):
    ticket_id: str


class EditTicketStages(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stages: list[TicketStageDefinition] = Field(
        min_length=1,
        max_length=20,
        description="Complete desired stage list in column order, including unchanged fields.",
    )
    moves: dict[TicketStageId, TicketStageId] = Field(
        default_factory=dict,
        max_length=20,
        description="Removed stage IDs mapped to surviving destinations for their tickets.",
    )

    @model_validator(mode="after")
    def validate_configuration(self) -> "EditTicketStages":
        UpdateTicketStages.model_validate(self.model_dump())
        return self


class AddTicketComment(CommentOnTicket):
    ticket_id: str


class LinkSessionTicket(TicketSessionLinkOptions):
    ticket_id: str
    session_id: str | None = Field(default=None, description="Omit to link this session.")


class LinkTickets(LinkRelatedTicket):
    ticket_id: str


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
    help_text = (
        "List tickets, oldest first, and this project's ordered stage definitions "
        "(IDs, names, instructions, appearance, and derived start/completion metadata). "
        "Colors and icons are visual only. "
        "The first stage starts new tickets; the last means complete. Read these before choosing "
        "a stage; do not assume fixed names. Use last_id as after while has_more is true."
    )
    arguments = ListTickets


class SysTicketGetTool(_TicketTool):
    tool_name = "sys_ticket_get"
    help_text = (
        "Read a ticket, its description/response contact, and pages of activity, sessions "
        "and related tickets, plus the project's current stage definitions. "
        "Compare the stage's meaning with actual work and the next step at progress and "
        "handoff checkpoints. "
        "Use kind=output to read PRs and other links."
    )
    arguments = GetTicket


class SysTicketCreateTool(_TicketTool):
    tool_name = "sys_ticket_create"
    help_text = (
        "Create a ticket in the project's first stage; only title is required. "
        "Tickets persist independently of sessions and cannot be deleted."
    )
    arguments = CreateTicket


class SysTicketUpdateTool(_TicketTool):
    tool_name = "sys_ticket_update"
    help_text = (
        "Edit a ticket; omit unchanged fields. Select stage_id from the current definitions in "
        "sys_ticket_list or sys_ticket_get and follow its instructions within authorized scope. "
        "The last stage means complete. Stage changes and "
        "instructions never grant approval or bypass human/permission prompts. "
        "Keep the goal and current context in description; append approaches, feedback and "
        "results as ordinary comments. "
        "Set lead_session_id to an already "
        "linked session (null clears it). Optional comment is appended atomically. "
        "Labels replace the whole key/value list: use labels with key needs-human plus a comment "
        "when a person must act, in any stage, and remove that label only when its request is "
        "resolved."
    )
    arguments = EditTicket


class SysTicketStagesUpdateTool(_TicketTool):
    tool_name = "sys_ticket_stages_update"
    help_text = (
        "Replace this project's board columns with the complete ordered stages list. "
        "Read current definitions with sys_ticket_list or sys_ticket_get first; keep stable "
        "IDs and unchanged names, instructions, colors and icons. Supply a unique ID for a "
        "new stage. The first stage starts tickets; the last means complete. Do not send "
        "initial_stage_id or is_complete. Removing an occupied stage requires moves from "
        "its ID to a surviving stage ID. Configuration and ticket moves commit together "
        "or not at all; tickets, history and response routing are preserved. Returns the "
        "saved definitions. This edits board structure; move individual tickets with "
        "sys_ticket_update. Stage edits do not start work or grant approvals."
    )
    arguments = EditTicketStages

    @classmethod
    def description(cls) -> str:
        return cls.help_text + " Scoped to this session's project."


class SysTicketCommentTool(_TicketTool):
    tool_name = "sys_ticket_comment"
    help_text = (
        "Append a comment or execution summary, optionally with outputs such as "
        "{type: pull_request, url: ...}. When progress changes the ticket's stage, use "
        "sys_ticket_update with stage_id and comment together instead. "
        "Activity cannot be edited or deleted."
    )
    arguments = AddTicketComment


class SysSessionLinkTicketTool(_TicketTool):
    tool_name = "sys_session_link_ticket"
    help_text = (
        "Attach or remove an existing same-project session's association with a ticket. "
        "Include involvement to explain its contribution; attach again to update it, "
        "omit to preserve it, or use an empty string to clear it. "
        "Does not create or restart sessions."
    )
    arguments = LinkSessionTicket


class SysTicketLinkTool(_TicketTool):
    tool_name = "sys_ticket_link"
    help_text = (
        "Attach or remove a symmetric related-ticket link within this project. "
        "Links have no type or automatic workflow effects. Explain the relationship in a comment."
    )
    arguments = LinkTickets


TICKET_TOOL_CLASSES = (
    SysTicketListTool,
    SysTicketGetTool,
    SysTicketCreateTool,
    SysTicketUpdateTool,
    SysTicketStagesUpdateTool,
    SysTicketCommentTool,
    SysSessionLinkTicketTool,
    SysTicketLinkTool,
)
TICKET_TOOLS = {tool.name(): tool for tool in TICKET_TOOL_CLASSES}
