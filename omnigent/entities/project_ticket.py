"""Small, shared contracts for project tickets and their append-only activity."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TicketStageId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
TicketStageColor = Literal["gray", "blue", "purple", "pink", "red", "orange", "amber", "green"]
TicketStageIcon = Literal[
    "circle-dashed",
    "circle",
    "circle-dot",
    "circle-check",
    "clipboard-list",
    "eye",
    "clock",
    "pause",
    "search",
    "flag",
    "wrench",
    "rocket",
]
TicketEventKind = Literal["created", "updated", "comment", "execution", "output"]
LEGACY_STATUS_CODES = {"open": 0, "in_progress": 1, "done": 2, "planning": 3, "review": 4}
DEFAULT_STAGE_NAMES = {
    "open": "Open",
    "in_progress": "Working",
    "review": "Review",
    "done": "Done",
}
EVENT_CODES = {"created": 0, "updated": 1, "comment": 2, "execution": 3, "output": 4}
NOTIFICATION_PENDING = 0
NOTIFICATION_QUEUED = 1
NOTIFICATION_SKIPPED = 2
# Pending, but batched into the orchestrator's next digest instead of waking it.
NOTIFICATION_DIGEST = 3
# A person's comment handed to the ticket's worker sessions; resolved by a worker reply.
NOTIFICATION_AWAITING_WORKER = 4
# A person's comment no worker answered in time; urgent for the orchestrator only.
NOTIFICATION_ESCALATED = 5
TICKET_PAGE_MAX = 100
STAGE_EDIT_TICKET_MAX = 500


class TicketStageDefinition(BaseModel):
    """User-editable stage fields shared by the board and agent tools."""

    model_config = ConfigDict(extra="forbid")

    id: TicketStageId
    name: str = Field(min_length=1, max_length=60)
    instructions: str = Field(default="", max_length=500)
    color: TicketStageColor | None = Field(
        default=None, description="Visual color only; null uses the default appearance."
    )
    icon: TicketStageIcon | None = Field(
        default=None, description="Visual icon only; null uses the default appearance."
    )

    @field_validator("name", "instructions")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_name(self) -> TicketStageDefinition:
        if not self.name:
            raise ValueError("stage name must not be empty")
        return self


class TicketStage(TicketStageDefinition):
    is_complete: bool = Field(
        default=False, description="Derived from the final stage's position."
    )


class TicketStages(BaseModel):
    """One project-owned configuration document, not a transition engine."""

    model_config = ConfigDict(extra="forbid")

    stages: list[TicketStage] = Field(min_length=1, max_length=20)
    initial_stage_id: TicketStageId | None = Field(
        default=None, description="Derived from the first stage; supplied values are ignored."
    )

    @model_validator(mode="after")
    def validate_stages(self) -> TicketStages:
        if len({stage.id for stage in self.stages}) != len(self.stages):
            raise ValueError("stage IDs must be unique")
        if len({stage.name.casefold() for stage in self.stages}) != len(self.stages):
            raise ValueError("stage names must be unique")
        self.initial_stage_id = self.stages[0].id
        self.stages = [
            stage.model_copy(update={"is_complete": index == len(self.stages) - 1})
            for index, stage in enumerate(self.stages)
        ]
        return self


class UpdateTicketStages(TicketStages):
    moves: dict[TicketStageId, TicketStageId] = Field(default_factory=dict, max_length=20)

    @model_validator(mode="after")
    def validate_moves(self) -> UpdateTicketStages:
        surviving = {stage.id for stage in self.stages}
        if any(
            source in surviving or target not in surviving for source, target in self.moves.items()
        ):
            raise ValueError("move targets must survive and move sources must be removed stages")
        return self


def default_ticket_stages() -> TicketStages:
    return TicketStages(
        stages=[
            TicketStage(id=stage_id, name=name) for stage_id, name in DEFAULT_STAGE_NAMES.items()
        ],
    )


class TicketLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    value: str | None = Field(default=None, max_length=256)

    @field_validator("key")
    @classmethod
    def trim_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("label key must not be empty")
        return value.strip()


_ARTIFACT_PATH = re.compile(
    r"/v1/projects/([0-9a-fA-F-]{32,36})/files/([0-9a-fA-F-]{32,36})(?:/content)?"
)


def _hex(value: str) -> str:
    return value.replace("-", "").lower()


class TicketOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["pull_request", "link", "artifact"]
    url: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def canonicalize(self) -> TicketOutput:
        if self.type == "artifact":
            # Project artifacts live on this server, so they are stored as its
            # relative content path rather than an external link.
            match = _ARTIFACT_PATH.fullmatch(self.url.strip())
            if match is None:
                raise ValueError(
                    "artifact outputs must be a project file path from sys_project_artifact_save"
                )
            self.url = f"/v1/projects/{_hex(match[1])}/files/{_hex(match[2])}/content"
            return self
        parts = urlsplit(self.url.strip())
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("outputs must be HTTP or HTTPS links")
        if parts.username or parts.password or re.search(r"\s", self.url):
            raise ValueError("output links must not contain credentials or whitespace")
        if self.type == "pull_request":
            match = re.match(
                r"(.+/(?:pull|pull-requests|merge_requests)/[1-9][0-9]*)(?:/.*)?$", parts.path
            )
            if match is None:
                raise ValueError("provide a pull request URL")
            parts = parts._replace(path=match[1], query="", fragment="")
        self.url = urlunsplit(parts._replace(netloc=parts.netloc.lower()))
        return self


class CreateTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=3000)
    labels: list[TicketLabel] = Field(default_factory=list, max_length=20)

    @field_validator("title")
    @classmethod
    def trim_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be empty")
        return value.strip()

    @field_validator("labels")
    @classmethod
    def unique_keys(cls, labels: list[TicketLabel]) -> list[TicketLabel]:
        if len({label.key for label in labels}) != len(labels):
            raise ValueError("label keys must be unique")
        return labels


# Room left in a ticket description for the "From the <project>" note on intake tickets.
INTAKE_DESCRIPTION_MAX_LENGTH = 2600


class FileIntakeTicket(BaseModel):
    """An agent in one project files a ticket on another project's board."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=INTAKE_DESCRIPTION_MAX_LENGTH)

    @field_validator("title")
    @classmethod
    def trim_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be empty")
        return value.strip()


TicketCommentAudience = Literal["human", "agents"]
AUDIENCE_DESCRIPTION = (
    "Who the comment is for. Omit for the default: comments from people and the project "
    "orchestrator are for the human, worker comments are agent notes that the board folds "
    "away. A worker sets human only for a question or request the human must answer."
)
FINAL_DESCRIPTION = (
    "Mark this comment as the ticket's final update: the plain-language result the board pins "
    "under the description while the ticket stays in its completed stage. Only valid when the "
    "ticket is, or this edit moves it, in the completed stage; always for the human."
)


def _require_human_final(final: bool, audience: str | None) -> None:
    if final and audience == "agents":
        raise ValueError("a final update is for the human; omit audience or set it to human")


class UpdateTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=3000)
    stage_id: TicketStageId | None = Field(
        default=None,
        description="A stage ID from this project's ticket list or stage definitions.",
    )
    labels: list[TicketLabel] | None = Field(default=None, max_length=20)
    add_labels: list[TicketLabel] | None = Field(
        default=None,
        max_length=20,
        description=(
            "Labels to add, replacing any with the same key, applied to the ticket's current "
            "labels in one write so concurrent label edits survive. Not with labels."
        ),
    )
    remove_labels: list[str] | None = Field(
        default=None,
        max_length=20,
        description="Label keys to remove, applied in the same write. Not with labels.",
    )
    comment: str | None = Field(default=None, min_length=1, max_length=2000)
    audience: TicketCommentAudience | None = Field(default=None, description=AUDIENCE_DESCRIPTION)
    final: bool = Field(default=False, description=FINAL_DESCRIPTION)

    @model_validator(mode="after")
    def final_needs_comment(self) -> UpdateTicket:
        if self.final and self.comment is None:
            raise ValueError("final requires a comment")
        _require_human_final(self.final, self.audience)
        return self

    @model_validator(mode="after")
    def labels_or_label_changes(self) -> UpdateTicket:
        if self.labels is not None and self.has_label_changes:
            raise ValueError("send labels, or add_labels/remove_labels, not both")
        added = {label.key for label in self.add_labels or []}
        if len(added) != len(self.add_labels or []):
            raise ValueError("add_labels keys must be unique")
        if added & set(self.remove_labels or []):
            raise ValueError("a label key can't be both added and removed")
        return self

    @property
    def has_label_changes(self) -> bool:
        return self.add_labels is not None or self.remove_labels is not None

    def labels_after(self, current: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """The full label list this edit leaves on a ticket whose labels are *current*.

        ``None`` when it doesn't touch labels. ``add_labels`` replace a same-key label in
        place and otherwise append; ``remove_labels`` drop keys.

        :raises ValueError: when the result has more than 20 labels.
        """
        if self.labels is not None:
            return [label.model_dump(exclude_none=True) for label in self.labels]
        if not self.has_label_changes:
            return None
        added = {label.key: label.model_dump(exclude_none=True) for label in self.add_labels or []}
        removed = set(self.remove_labels or [])
        result: list[dict[str, Any]] = []
        for label in current or []:
            key = label.get("key") if isinstance(label, dict) else None
            if key in removed:
                continue
            result.append(added.pop(key) if key in added else label)
        result.extend(added.values())
        if len(result) > 20:
            raise ValueError("a ticket can have at most 20 labels")
        return result

    @field_validator("title", "comment")
    @classmethod
    def trim_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("text must not be empty")
        return value.strip() if value is not None else None

    @field_validator("labels")
    @classmethod
    def unique_keys(cls, labels: list[TicketLabel] | None) -> list[TicketLabel] | None:
        return None if labels is None else CreateTicket.unique_keys(labels)


class MarkTicketSeen(BaseModel):
    """The latest activity position the viewer has seen; later positions read as new."""

    model_config = ConfigDict(extra="forbid")

    seen_position: int = Field(ge=-1, le=2**31 - 1)


class CommentOnTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["comment", "execution"] = "comment"
    outputs: list[TicketOutput] = Field(default_factory=list, max_length=10)
    audience: TicketCommentAudience | None = Field(default=None, description=AUDIENCE_DESCRIPTION)
    final: bool = Field(default=False, description=FINAL_DESCRIPTION)

    @model_validator(mode="after")
    def final_is_for_human(self) -> CommentOnTicket:
        _require_human_final(self.final, self.audience)
        return self

    @field_validator("text")
    @classmethod
    def trim_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("comment must not be empty")
        return value.strip()


class TicketSessionLinkOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["attach", "remove"] = "attach"
    involvement: str | None = Field(
        default=None,
        max_length=300,
        description="Brief contribution to this ticket. Omit to retain it; empty clears it.",
    )

    @field_validator("involvement")
    @classmethod
    def trim_involvement(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def involvement_only_on_attach(self) -> TicketSessionLinkOptions:
        if self.action == "remove" and self.involvement is not None:
            raise ValueError("involvement is only valid when attaching or updating a link")
        return self


class LinkTicketSession(TicketSessionLinkOptions):
    session_id: str


class LinkRelatedTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    related_ticket_id: str
    action: Literal["attach", "remove"] = "attach"


@dataclass(frozen=True)
class TicketActor:
    user_id: str | None
    session_id: str | None = None
    agent_id: str | None = None


class ImportPullRequest(BaseModel):
    """Body for importing an outside GitHub pull request as a ticket."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        min_length=1,
        max_length=2048,
        description="The PR's URL, e.g. https://github.com/<owner>/<repo>/pull/<n>.",
    )
    record: dict[str, Any] | None = Field(
        default=None,
        description="A recorded GitHub pull-request object to use instead of reading GitHub, "
        "for offline intake and dry runs. It must match the URL.",
    )


class RevertDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shas: list[str] | None = Field(
        default=None,
        max_length=50,
        description="Full SHAs of the landed commits to revert. Omit to revert the "
        "ticket's commits in the unpushed range, as the push prefix maps them.",
    )


class LandTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha: str = Field(
        min_length=40,
        max_length=40,
        description="The full 40-character commit SHA the Reviewer approved.",
    )
    dry_run: bool = Field(
        default=False,
        description="Only check every landing gate (approval, risk-ok, owner-ok, kind and "
        "product-ok, fast-forward) and report, without queueing. Works when the queue is "
        "off; run it before landing by hand.",
    )
