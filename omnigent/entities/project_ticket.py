"""Small, shared contracts for project tickets and their append-only activity."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TicketStatus = Literal["open", "planning", "in_progress", "review", "done"]
TicketEventKind = Literal["created", "updated", "comment", "execution", "output"]
STATUS_CODES = {"open": 0, "in_progress": 1, "done": 2, "planning": 3, "review": 4}
STATUS_LABELS = {
    "open": "Open",
    "planning": "Planning",
    "in_progress": "Working",
    "review": "Review",
    "done": "Done",
}
EVENT_CODES = {"created": 0, "updated": 1, "comment": 2, "execution": 3, "output": 4}
NOTIFICATION_PENDING = 0
NOTIFICATION_QUEUED = 1
NOTIFICATION_SKIPPED = 2
TICKET_PAGE_MAX = 100


class TicketTag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    value: str | None = Field(default=None, max_length=256)

    @field_validator("key")
    @classmethod
    def trim_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tag key must not be empty")
        return value.strip()


class TicketOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["pull_request", "link"]
    url: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def canonicalize(self) -> TicketOutput:
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
    tags: list[TicketTag] = Field(default_factory=list, max_length=20)

    @field_validator("title")
    @classmethod
    def trim_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be empty")
        return value.strip()

    @field_validator("tags")
    @classmethod
    def unique_keys(cls, tags: list[TicketTag]) -> list[TicketTag]:
        if len({tag.key for tag in tags}) != len(tags):
            raise ValueError("tag keys must be unique")
        return tags


class UpdateTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=3000)
    status: TicketStatus | None = None
    tags: list[TicketTag] | None = Field(default=None, max_length=20)
    completion_summary: str | None = Field(default=None, min_length=1, max_length=1500)
    plan: str | None = Field(default=None, max_length=3000)
    review_summary: str | None = Field(default=None, max_length=1500)
    lead_session_id: str | None = Field(
        default=None, description="An already linked session to contact; null clears the lead."
    )
    comment: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("title", "completion_summary", "comment")
    @classmethod
    def trim_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("text must not be empty")
        return value.strip() if value is not None else None

    @field_validator("tags")
    @classmethod
    def unique_keys(cls, tags: list[TicketTag] | None) -> list[TicketTag] | None:
        return None if tags is None else CreateTicket.unique_keys(tags)

    @model_validator(mode="after")
    def require_completion(self) -> UpdateTicket:
        if self.status == "done" and not self.completion_summary:
            raise ValueError("marking Done requires a completion summary")
        if self.status == "review" and not (self.review_summary or "").strip():
            raise ValueError("entering Review requires a review summary")
        return self


class CommentOnTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["comment", "execution"] = "comment"
    outputs: list[TicketOutput] = Field(default_factory=list, max_length=10)

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
