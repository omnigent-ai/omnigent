"""Ticket tools share their validation contracts with the project board API."""

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities.project import (
    MANDATE_PROPOSAL_MAX_BYTES,
    PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES,
    PROJECT_MANDATE_MAX_BYTES,
)
from omnigent.entities.project_kpi import SetProjectKpis
from omnigent.entities.project_ticket import (
    CommentOnTicket,
    CreateTicket,
    FileIntakeTicket,
    ImportPullRequest,
    LandTicketRequest,
    LinkRelatedTicket,
    RevertDraftRequest,
    TicketEventKind,
    TicketSessionLinkOptions,
    TicketStageDefinition,
    TicketStageId,
    UpdateTicket,
    UpdateTicketStages,
)
from omnigent.tools.base import Tool
from omnigent.tools.builtins.session_message import SESSION_MESSAGE_MAX_CHARS


class ListTickets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage_id: TicketStageId | None = Field(
        default=None, description="Filter by a stage ID from this project's definitions."
    )
    after: str | None = Field(default=None, description="Previous page's last_id.")
    limit: int = Field(default=50, ge=1, le=100)
    compact: bool = Field(
        default=True,
        description="Compact rows (no descriptions, truncated latest_update, stage IDs and "
        "names only). Set false for full tickets and stage instructions.",
    )
    include_guidance: bool = Field(
        default=False,
        description="Also return the ticket work guidance. Project sessions already have it "
        "in their instructions; request it only if yours lack it.",
    )


class GetTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: str
    after: int = Field(default=-1, ge=-1, description="Previous events page's last_position.")
    kind: TicketEventKind | None = None
    sessions_after: str | None = None
    related_after: str | None = None
    limit: int = Field(default=50, ge=1, le=100)
    include_description: bool = Field(
        default=True,
        description="False omits the description from the ticket and its events.",
    )
    include_stages: bool = Field(
        default=True, description="False omits the project's stage definitions."
    )
    include_guidance: bool = Field(
        default=False,
        description="Also return the ticket work guidance. Project sessions already have it "
        "in their instructions; request it only if yours lack it.",
    )


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
        max_length=PROJECT_MANDATE_MAX_BYTES,
        description=(
            "append: the addition in the user's terms, written as a standing rule for every "
            "agent, e.g. 'Run the UI tests before moving a ticket to review.' (at most "
            f"{MANDATE_PROPOSAL_MAX_BYTES} bytes). replace_section: the section's complete new "
            "text including its heading line, or empty to delete the section. replace_all: "
            "the complete new mandate."
        ),
    )
    mode: Literal["append", "replace_section", "replace_all"] = Field(
        default="append",
        description=(
            "append adds to the end. Use replace_section or replace_all to rewrite or remove "
            "rules that conflict or are superseded, instead of appending a contradiction."
        ),
    )
    section: str | None = Field(
        default=None,
        max_length=200,
        description="For replace_section: the exact title of one existing heading, e.g. 'Stages'.",
    )


_IDENTITY_NAME = Field(
    min_length=1,
    max_length=60,
    description="A clear role name, unique in the project, e.g. 'PM' or 'Designer'.",
)
_IDENTITY_PERSONA = Field(
    min_length=1,
    max_length=PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES,
    description=(
        "The complete persona, addressed to the identity in the second person: its role, what "
        "it owns and how it works, e.g. 'You are the project's PM. You own the roadmap and "
        "write ticket descriptions; you don't write code.'"
    ),
)


_ROLE_OWNS = Field(
    default=None,
    max_length=12,
    description="What the role owns and is accountable for, one short item each.",
)
_ROLE_DOES_NOT_OWN = Field(
    default=None,
    max_length=12,
    description="What the role leaves to others, one short item each.",
)


class CreateIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = _IDENTITY_NAME
    instructions: str = _IDENTITY_PERSONA
    owns: list[str] | None = _ROLE_OWNS
    does_not_own: list[str] | None = _ROLE_DOES_NOT_OWN


class UpdateIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: str = Field(
        min_length=1,
        max_length=64,
        description="The identity's current name (case-insensitive) or ID.",
    )
    name: str | None = Field(default=None, min_length=1, max_length=60, description="New name.")
    instructions: str | None = Field(
        default=None,
        min_length=1,
        max_length=PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES,
        description="The complete replacement persona.",
    )
    owns: list[str] | None = _ROLE_OWNS
    does_not_own: list[str] | None = _ROLE_DOES_NOT_OWN

    @model_validator(mode="after")
    def _has_change(self) -> "UpdateIdentity":
        if all(
            value is None for value in (self.name, self.instructions, self.owns, self.does_not_own)
        ):
            raise ValueError("Give a new name, instructions or scope")
        return self


class ListIdentities(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenIdentitySession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: str = Field(
        min_length=1,
        max_length=64,
        description="The identity's name (case-insensitive), e.g. 'PM', or its ID.",
    )
    message: str | None = Field(
        default=None,
        min_length=1,
        max_length=32_000,
        description=(
            "Optional self-contained message delivered like sys_session_message, e.g. the "
            "ticket ID, goal, and how to report back."
        ),
    )


class _TicketTool(Tool):
    tool_name: ClassVar[str]
    help_text: ClassVar[str]
    arguments: ClassVar[type[BaseModel]]
    #: Only reads project state, so harnesses may run it without an approval prompt.
    read_only: ClassVar[bool] = False

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
    read_only = True
    help_text = (
        "List tickets, oldest first, and this project's ordered stages. By default rows are "
        "compact (ID, title, stage, labels, truncated latest update, updated_at, running "
        "agents) and stages carry only ID, name and completion; pass compact=false for "
        "descriptions and full stage definitions, or read one with sys_ticket_get. "
        "The first stage starts new tickets; the last means complete. Read these before choosing "
        "a stage; do not assume fixed names. Filtering by the last stage lists newest first. "
        "Use last_id as after while has_more is true."
    )
    arguments = ListTickets


class SysTicketGetTool(_TicketTool):
    tool_name = "sys_ticket_get"
    read_only = True
    help_text = (
        "Read a ticket, its description, and pages of activity, sessions "
        "and related tickets, plus the project's current stage definitions. "
        "Compare the stage's meaning with actual work and the next step at progress and "
        "handoff checkpoints. "
        "The ticket's product_approvers lists the only people whose product-ok counts; ask "
        "all of them, by those names, when it needs product approval, its requester "
        "included (only risk-ok excludes the requester). "
        "Use kind=output to read PRs and other links. For routine re-reads, pass "
        "include_description=false and include_stages=false to keep the response small."
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
        "in any stage, and remove that label only when its request is resolved. A comment or "
        "sign-off label such as risk-ok from a person the ask is routed to clears it "
        "automatically; add it back with a fresh "
        "question if that comment did not resolve it. Set audience human on that ask; other "
        "worker comments stay agent notes by default."
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


class LandTicket(LandTicketRequest):
    ticket_id: str


class SysTicketLandTool(_TicketTool):
    tool_name = "sys_ticket_land"
    help_text = (
        "Submit a Reviewer-approved commit to the project's landing queue. The server lands "
        "it only if the ticket's `approved` label is exactly this full SHA, any `risk` label "
        "has an owner `risk-ok`, an imported PR has the owner's `owner-ok`, a `kind=feature` "
        "ticket the owner didn't request has a product approver's `product-ok`, the SHA "
        "fast-forwards the integration branch and, when the project requires it, the "
        "`verified` label is that SHA (or the identical patch rebased over commits that don't "
        "touch its files); otherwise it refuses and says why (for example, "
        "rebase and get re-approval). Accepted lands are batched: fast-forward, web rebuild if "
        "needed, one server restart. The ticket then moves to the landed stage (Implemented) and "
        "you're told to verify live. Never pushes. Refused when the project's landing queue is "
        "off; then hand the branch and SHA to your coordinator. With dry_run it only runs "
        "those gates and reports, even when the queue is off: run it before landing by hand."
    )
    arguments = LandTicket


class PushPrefix(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SysTicketPushPrefixTool(_TicketTool):
    tool_name = "sys_ticket_push_prefix"
    read_only = True
    help_text = (
        "Report the longest prefix of the landing branch's unpushed commits (since the "
        "remote-tracking branch; fetch first) that may be pushed. Each commit maps to the "
        "ticket whose approved or verified SHA is the first at or after it. The prefix holds "
        "only tickets in Verified or later with all their unpushed commits, or tickets whose "
        "commits a later Verified revert named in their `reverted-by` label fully undoes. "
        "Returns pushable_sha, the stop commit with its ticket and reason, the Verified "
        "tickets held behind it, and every commit's status. Never pushes."
    )
    arguments = PushPrefix


class RevertDraft(RevertDraftRequest):
    ticket_id: str


class SysTicketRevertDraftTool(_TicketTool):
    tool_name = "sys_ticket_revert_draft"
    help_text = (
        'Draft `Revert "<subject>"` commits for a landed ticket, newest first, on top of the '
        "integration tip, and create them on a new `ptb/revert-...` branch. By default it "
        "reverts the ticket's unpushed commits; pass shas for others. Built with `git "
        "merge-tree` and `git commit-tree`: no worktree is written and no existing branch "
        "moves. On a conflict it creates nothing and reports the files and the later commits "
        "that touched them. The revert is a normal change: check it out in a worktree, "
        "verify it and send it for review."
    )
    arguments = RevertDraft


class SysTicketImportPrTool(_TicketTool):
    tool_name = "sys_ticket_import_pr"
    help_text = (
        "File an outside GitHub pull request as a ticket in this project's first stage. The "
        "server reads the PR (read-only; nothing is posted to GitHub), records its author as "
        "the requester `github:<login>`, adds the PR link as an output and returns the "
        "ticket; importing the same PR again returns the existing ticket. Imported tickets "
        "land only with the Reviewer's approval plus the owner's `owner-ok` for the same SHA. "
        "Don't run the PR's code until the Reviewer has read it."
    )
    arguments = ImportPullRequest


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
        "Propose a change to this project's mandate, the owner's standing guidance loaded "
        "into every agent session in the project: an addition, a replacement or deletion of "
        "one section (by its heading title), or a whole new mandate. Use it when the user "
        "asks for durable project-wide feedback to be remembered, such as 'always', 'every "
        "change' or 'from now on', or to clean up conflicting rules. The owner sees it as a "
        "diff; it changes nothing until the owner accepts it in the project's Mandate dialog, "
        "so tell the user it is waiting for them. Never claim it is in effect. A replacement "
        "goes stale, and can't be accepted, if its text changes first. Agents cannot edit "
        "the mandate directly. Scoped to this session's project."
    )
    arguments = ProposeMandateAddition

    @classmethod
    def description(cls) -> str:
        return cls.help_text


class SysProjectIdentityCreateTool(_TicketTool):
    tool_name = "sys_project_identity_create"
    help_text = (
        "Create a project identity: a named persistent role such as a PM or Designer whose "
        "persona is added to every session working as it. When someone asks for a new role, "
        "persona, teammate or team member, use this, not sys_session_create or a sub-agent; "
        "if it's unclear whether they want a lasting role or a one-off helper, ask first. "
        "Also create one when a recurring workstream needs an owner. Give it a clear name and "
        "complete persona text, and fill in what it owns and doesn't own. "
        "It takes effect at once and appears in the project's Team dialog; tell the user "
        "what you created. Check "
        "sys_project_identity_list first and reuse a suitable role. Names are unique and 'Lead' "
        "and 'Orchestrator' are reserved; if the name is taken it returns the existing identity "
        "with existing=true and instructions_differ, and changes nothing. Start its session "
        "with sys_project_identity_session."
    )
    arguments = CreateIdentity


class SysProjectIdentityUpdateTool(_TicketTool):
    tool_name = "sys_project_identity_update"
    help_text = (
        "Rename an agent-created project identity or replace its persona. Identities the "
        "owner created are owner-only and refuse agent edits, and only the owner can delete "
        "identities. Running sessions keep the old persona until they restart."
    )
    arguments = UpdateIdentity


class SysProjectIdentityListTool(_TicketTool):
    tool_name = "sys_project_identity_list"
    read_only = True
    help_text = (
        "List this project's identities (the Team, excluding the built-in Lead): each one's "
        "name, ID, persona, whether an agent or the owner created it, and its session ID and "
        "state. Read it before creating an identity or staffing a workstream, so you reuse a "
        "suitable role instead of creating a duplicate."
    )
    arguments = ListIdentities


class SysProjectIdentitySessionTool(_TicketTool):
    tool_name = "sys_project_identity_session"
    help_text = (
        "Open the session of one of this project's existing identities (roles such as 'PM') "
        "to delegate a workstream to it. Returns its one current session; the server recreates "
        "it with the identity's persona if it was deleted or archived, and never starts a "
        "second one. Call it again for the current session_id if a message to an old one "
        "fails with 'Session not found'. "
        "With message, also delivers that message to it like sys_session_message (queued if "
        "it is busy). Keep talking to it later with sys_session_message on the returned "
        "session_id. The built-in Lead is not an identity here; message it as `lead`. To add "
        "a role, use sys_project_identity_create."
    )
    arguments = OpenIdentitySession


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
        "Save a workspace file (plan, report, document, screenshot, log or other evidence, "
        "up to 50 MB) to "
        "this project's Files so the user can open it from the project and its tickets. "
        "With ticket_id, also posts a ticket comment with the file as an artifact output. "
        "Returns the artifact's id and url; pass {type: artifact, url} in a later "
        "sys_ticket_comment's outputs to attach it elsewhere. Prefer this over publishing to "
        "external services or reporting local-only paths."
    )
    arguments = SaveProjectArtifact


class ViewProjectFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(
        min_length=1,
        max_length=2048,
        description=(
            "The file's content path as it appears in a ticket's description, comments or "
            "outputs: /v1/projects/<project>/ticket-attachments/<id>/content or "
            "/v1/projects/<project>/files/<id>/content. A full URL with that path also works."
        ),
    )
    save_path: str | None = Field(
        default=None,
        max_length=4096,
        description="Optional workspace-relative path to also write the file to, e.g. 'bug.png'.",
    )


class SysProjectFileViewTool(_TicketTool):
    tool_name = "sys_project_file_view"
    read_only = True
    help_text = (
        "Open an image or other file attached to this project's tickets: a screenshot pasted "
        "into a ticket's description or comments (/v1/projects/<project>/ticket-attachments/"
        "<id>/content) or a project file (/v1/projects/<project>/files/<id>/content). Images "
        "come back for you to see, text files as text. Use it instead of fetching those paths "
        "over HTTP, which has no credentials in your environment. Pass save_path to also "
        "write the file into your workspace, e.g. to open a video or archive with other tools."
    )
    arguments = ViewProjectFile


_KPI_ROLE = Field(
    default=None,
    min_length=1,
    max_length=64,
    description=(
        "A role's name or ID to use that role's own KPIs, measured over its tickets "
        "and sessions; omit for the project's."
    ),
)


class GetProjectKpis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window: Literal["7d", "30d", "all"] = Field(
        default="7d", description="The UTC window values are measured over, as on the Summary."
    )
    role: str | None = _KPI_ROLE


class SetKpis(SetProjectKpis):
    role: str | None = _KPI_ROLE


class SysProjectKpisGetTool(_TicketTool):
    tool_name = "sys_project_kpis_get"
    read_only = True
    help_text = (
        "Read this project's KPIs, or one role's with role (counted over tickets assigned to "
        "the role's current name and its sessions): each one's name, metric, why it matters, "
        "target and "
        "direction, with its current value and whether it meets the target over the window. "
        "Also returns the current revision, who set it, and the metrics a KPI may track."
    )
    arguments = GetProjectKpis


class SysProjectTelemetryGetTool(_TicketTool):
    tool_name = "sys_project_telemetry_get"
    read_only = True
    help_text = (
        "Read how people work in this project over a UTC window, from the project's own data: "
        "how people start work (sessions vs tickets), how they use the Lead and roles, how the "
        "team of roles was built, and what the project shipped (tickets, PRs, rework, review "
        "rounds, stalled work). Totals only; per-person rows are never returned to agents. "
        "Numbers that aren't measured on this server are null."
    )
    arguments = GetProjectKpis


class SysProjectKpisSetTool(_TicketTool):
    tool_name = "sys_project_kpis_set"
    help_text = (
        "Replace this project's KPIs, or one role's with role, with a complete new set "
        "(3-4 is typical), stored as a "
        "new revision; earlier revisions are kept. Only the project's Lead may call it, and "
        "only when the owner asked for the change. Read the KPIs first and pass their "
        "revision as base_revision so a concurrent edit isn't overwritten."
    )
    arguments = SetKpis


class FileTicketInProject(FileIntakeTicket):
    project: str = Field(
        min_length=1,
        max_length=160,
        description="The other project's name or ID, as it appears in your project list.",
    )


class SysProjectTicketFileTool(_TicketTool):
    tool_name = "sys_project_ticket_file"
    help_text = (
        "File a ticket on another project's board, one shared with the person you work for "
        "(they own it or are a member). Use it to ask that project for help or report "
        "something to it, instead of opening a session inside it. The ticket starts in its "
        "first stage, marked as coming from your project; that project's Lead and its own "
        "rules and approvals take it from there. You can't set labels or read its board, and "
        "you get back only the new ticket's ID. Up to 10 per hour into one project."
    )
    arguments = FileTicketInProject

    @classmethod
    def description(cls) -> str:
        return cls.help_text


class MessageOtherLead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        description="The other project's name or ID, as it appears in your project list. "
        "Omit when replying.",
    )
    reply_to: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="The message ID shown in a message another project's Lead sent you; "
        "your reply goes back to that project's Lead.",
    )
    message: str = Field(
        min_length=1,
        max_length=SESSION_MESSAGE_MAX_CHARS,
        description="Self-contained text; the other Lead sees nothing else of your project.",
    )

    @model_validator(mode="after")
    def one_target(self) -> "MessageOtherLead":
        if (self.project is None) == (self.reply_to is None):
            raise ValueError("Pass either project, to start a conversation, or reply_to")
        return self


class SysProjectLeadMessageTool(_TicketTool):
    tool_name = "sys_project_lead_message"
    help_text = (
        "As your project's Lead, message another project's Lead: one shared with the person "
        "you work for (they own it or are a member). Use it for questions, such as how that "
        "project handles something, and file an intake ticket with sys_project_ticket_file "
        "for work you want done. The message is marked as coming from your project's Lead; "
        "its reply arrives in your chat with a message ID. Pass reply_to with that ID to "
        "answer a message another project's Lead sent you. Gives no access to the other "
        "project's board or sessions. Up to 10 per hour into one project."
    )
    arguments = MessageOtherLead

    @classmethod
    def description(cls) -> str:
        return cls.help_text


TICKET_TOOL_CLASSES = (
    SysTicketListTool,
    SysTicketGetTool,
    SysTicketCreateTool,
    SysTicketUpdateTool,
    SysTicketStagesUpdateTool,
    SysTicketCommentTool,
    SysTicketLandTool,
    SysTicketPushPrefixTool,
    SysTicketRevertDraftTool,
    SysTicketImportPrTool,
    SysSessionLinkTicketTool,
    SysTicketLinkTool,
    SysProjectMandateProposeTool,
    SysProjectIdentityListTool,
    SysProjectIdentityCreateTool,
    SysProjectIdentityUpdateTool,
    SysProjectIdentitySessionTool,
    SysProjectArtifactSaveTool,
    SysProjectFileViewTool,
    SysProjectKpisGetTool,
    SysProjectKpisSetTool,
    SysProjectTicketFileTool,
    SysProjectTelemetryGetTool,
    SysProjectLeadMessageTool,
)
TICKET_TOOLS = {tool.name(): tool for tool in TICKET_TOOL_CLASSES}
READ_ONLY_TICKET_TOOLS: tuple[str, ...] = tuple(
    tool.name() for tool in TICKET_TOOL_CLASSES if tool.read_only
)
