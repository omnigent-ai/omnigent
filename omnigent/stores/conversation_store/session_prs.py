"""Bounded, provider-independent pull request metadata for session snapshots."""

from __future__ import annotations

import json
import time
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator


def validate_session_pr_revision(value: object) -> int:
    """Reject revisions that could pin a session ahead of its runners' clocks."""
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise ValueError("Pull request revision must be a positive 64-bit integer")
    if value > time.time_ns() + 5 * 60 * 1_000_000_000:
        raise ValueError("Pull request revision must not be more than five minutes in the future")
    return value


class _PullRequestSnapshot(BaseModel):
    provider: str = Field(default="github", min_length=1, max_length=128)
    host: str = Field(min_length=1, max_length=255)
    repository: str = Field(min_length=1, max_length=1024)
    number: int = Field(gt=0, strict=True)
    url: str = Field(max_length=4096)
    relationship: Literal["created", "worked_on", "attached", "inferred"]
    source: str = Field(min_length=1, max_length=128)
    first_seen_at: float = Field(ge=0, allow_inf_nan=False)
    last_seen_at: float = Field(ge=0, allow_inf_nan=False)
    title: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def validate_identity(self) -> _PullRequestSnapshot:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.netloc.lower() != self.host.lower()
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path.strip("/")
            or parsed.query
            or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in self.url)
        ):
            raise ValueError("Pull request URL must be an HTTPS URL on its recorded host")
        return self


def validate_session_prs(value: object) -> list[dict[str, Any]]:
    """Validate identities without loading a host's provider plugins or credentials."""
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("Session pull requests must be a list of at most 100 entries")
    entries = [_PullRequestSnapshot.model_validate(entry).model_dump() for entry in value]
    if len(json.dumps(entries, ensure_ascii=False).encode("utf-8")) > 128 * 1024:
        raise ValueError("Session pull requests must not exceed 128 KiB")
    return list({entry["url"]: entry for entry in entries}.values())
