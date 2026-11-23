"""Ticket tools share their validation contracts with the project board API."""

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities.project import MANDATE_PROPOSAL_MAX_BYTES
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


class ProposeMandateAddition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(
        min_length=1,
        max_length=MANDATE_PROPOSAL_MAX_BYTES,
        description=(
            "The addition in the user's terms, written as a standing rule for every agent in "
            "the project, e.g. 'Run the UI tests before moving a ticket to review.'"
        ),
    )


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
        "a stage; do not assume fixed names. Filtering by the last stage lists newest first. "
        "Use last_id as after while has_more is true."
    )
    arguments = ListTickets


class SysTicketGetTool(_TicketTool):
    tool_name = "sys_ticket_get"
    help_text = (
        "Read a ticket, its description, and pages of activity, sessions "
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
        "Tickets persist independently of sessions and cannot be deleted. Use them for "
        "persistent work that moves through stages, not to record a conversation, question "
        "or quick task."
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
        "Optional comment is appended atomically. "
        "Labels replace the whole key/value list: use labels with key needs-human plus a comment "
        "with the exact ask and a link to the responsible worker's chat when a person must act, "
        "in any stage, and remove that label only when its request is resolved. Set audience "
        "human on that ask; other worker comments stay agent notes by default."
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
        "or not at all; tickets, history and session links are preserved. Returns the "
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
        "{type: pull_request, url: ...} or a project file as {type: artifact, url: ...}. "
        "When progress changes the ticket's stage, use "
        "sys_ticket_update with stage_id and comment together instead. "
        "Worker comments default to terse agent notes that the board folds away; the "
        "orchestrator's and people's comments are for the human. "
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


class SysProjectMandateProposeTool(_TicketTool):
    tool_name = "sys_project_mandate_propose"
    help_text = (
        "Propose an addition to this project's mandate, the owner's standing guidance loaded "
        "into every agent session in the project. Use it when the user asks for durable "
        "project-wide feedback to be remembered, such as 'always', 'every change' or 'from "
        "now on'. The proposal changes nothing until the owner accepts it in the project's "
        "Mandate dialog, so tell the user it is waiting for them. Never claim it is in "
        "effect. Agents cannot edit the mandate directly. Scoped to this session's project."
    )
    arguments = ProposeMandateAddition

    @classmethod
    def description(cls) -> str:
        return cls.help_text


class SaveProjectArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(
        min_length=1,
        max_length=4096,
        description="Workspace-relative file to save, e.g. 'out/proposal.md' or 'shot.png'.",
    )
    title: str | None = Field(default=None, max_length=200, description="Display title.")
    ticket_id: str | None = Field(
        default=None, description="Ticket to attach it to with a comment carrying the artifact."
    )
    comment: str | None = Field(
        default=None,
        max_length=2000,
        description="Comment posted with the attachment when ticket_id is set.",
    )


class SysProjectArtifactSaveTool(_TicketTool):
    tool_name = "sys_project_artifact_save"
    help_text = (
        "Save a workspace file (document, screenshot, log or other evidence, up to 50 MB) to "
        "this project's Files so the user can open it from the project and its tickets. "
        "With ticket_id, also posts a ticket comment with the file as an artifact output. "
        "Returns the artifact's id and url; pass {type: artifact, url} in a later "
        "sys_ticket_comment's outputs to attach it elsewhere. Prefer this over publishing to "
        "external services or reporting local-only paths."
    )
    arguments = SaveProjectArtifact


TICKET_TOOL_CLASSES = (
    SysTicketListTool,
    SysTicketGetTool,
    SysTicketCreateTool,
    SysTicketUpdateTool,
    SysTicketStagesUpdateTool,
    SysTicketCommentTool,
    SysSessionLinkTicketTool,
    SysTicketLinkTool,
    SysProjectMandateProposeTool,
    SysProjectArtifactSaveTool,
)
TICKET_TOOLS = {tool.name(): tool for tool in TICKET_TOOL_CLASSES}
