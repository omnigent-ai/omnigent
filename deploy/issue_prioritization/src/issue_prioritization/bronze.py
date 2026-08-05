from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from issue_prioritization.classification import Classification, IssueContent
from issue_prioritization.domain import Issue, Priority


@dataclass(frozen=True)
class BronzeIssue:
    number: int
    title: str
    body: str
    url: str
    author: str
    labels: tuple[str, ...]
    created_at: datetime
    upvote_count: int
    duplicate_count: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BronzeIssue:
        return cls(
            number=int(_first(value, "number", "issue_number")),
            title=str(_first(value, "title", default="")),
            body=str(_first(value, "body", default="") or ""),
            url=str(_first(value, "html_url", "url", default="")),
            author=str(_first(value, "author_login", "user_login", "author", default="")),
            labels=_labels(_first(value, "labels", "label_names", default=())),
            created_at=_timestamp(_first(value, "created_at")),
            upvote_count=_upvote_count(value),
            duplicate_count=max(0, int(_first(value, "duplicate_count", default=0) or 0)),
        )

    def content(self) -> IssueContent:
        return IssueContent(
            number=self.number,
            title=self.title,
            body=self.body,
            labels=self.labels,
            author=self.author,
        )

    def to_issue(self, classification: Classification, now: datetime) -> Issue:
        return Issue(
            number=self.number,
            title=self.title,
            url=self.url,
            issue_type=classification.issue_type,
            severity=classification.severity,
            area_keys=classification.area_keys,
            component_labels=classification.component_labels,
            duplicate_count=self.duplicate_count,
            upvote_count=self.upvote_count,
            current_priority=_current_priority(self.labels),
            needs_info="needs-info" in self.labels,
            age_days=max(0, (now - self.created_at).days),
        )


def _first(value: Mapping[str, object], *names: str, default: object = None) -> object:
    for name in names:
        if name in value:
            return value[name]
    return default


def _labels(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            return _labels(json.loads(value))
        except json.JSONDecodeError:
            return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, Mapping):
        return tuple(str(key) for key in value)
    if not isinstance(value, (list, tuple)):
        return ()
    labels = []
    for item in value:
        if isinstance(item, Mapping):
            name = item.get("name")
            if name:
                labels.append(str(name))
        else:
            labels.append(str(item))
    return tuple(labels)


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC)


def _upvote_count(value: Mapping[str, object]) -> int:
    direct = _first(
        value,
        "upvote_count",
        "thumbs_up_count",
        "reactions_plus_one_count",
    )
    if direct is not None:
        return max(0, int(direct))
    reactions = value.get("reactions")
    if isinstance(reactions, str):
        try:
            reactions = json.loads(reactions)
        except json.JSONDecodeError:
            return 0
    if isinstance(reactions, Mapping):
        return max(0, int(reactions.get("+1", 0)))
    return 0


def _current_priority(labels: tuple[str, ...]) -> Priority | None:
    return next((priority for priority in Priority if priority.value in labels), None)
