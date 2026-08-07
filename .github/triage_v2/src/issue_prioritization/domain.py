from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class IssueType(StrEnum):
    BUG = "bug"
    ENHANCEMENT = "enhancement"
    DOCUMENTATION = "documentation"

    @classmethod
    def parse(cls, value: object) -> IssueType:
        normalized = str(value).strip().casefold()
        aliases = {
            "bug": cls.BUG,
            "feature": cls.ENHANCEMENT,
            "enhancement": cls.ENHANCEMENT,
            "docs": cls.DOCUMENTATION,
            "documentation": cls.DOCUMENTATION,
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError(f"unsupported issue type: {value!r}") from exc

    @property
    def label(self) -> str:
        return {
            IssueType.BUG: "Bug",
            IssueType.ENHANCEMENT: "Feature",
            IssueType.DOCUMENTATION: "Docs",
        }[self]


class Severity(StrEnum):
    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"


class Priority(StrEnum):
    P0 = "P0-critical"
    P1 = "P1-high"
    P2 = "P2-medium"
    P3 = "P3-low"


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    url: str
    issue_type: IssueType
    severity: Severity
    area_keys: tuple[str, ...] = ()
    component_labels: tuple[str, ...] = ()
    classification_reasoning: str = ""
    duplicate_count: int = 0
    upvote_count: int = 0
    current_priority: Priority | None = None
    needs_info: bool = False
    is_ready: bool = False
    age_days: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Issue:
        current_priority = value.get("current_priority")
        return cls(
            number=int(value["number"]),
            title=str(value.get("title", "")),
            url=str(value.get("url", "")),
            issue_type=IssueType.parse(value["type"]),
            severity=Severity(str(value["severity"])),
            area_keys=_string_tuple(value.get("area_keys", ())),
            component_labels=_string_tuple(value.get("component_labels", ())),
            classification_reasoning=str(
                value.get("classification_reasoning", value.get("reasoning", ""))
            ),
            duplicate_count=max(0, int(value.get("duplicate_count", 0))),
            upvote_count=max(0, int(value.get("upvote_count", 0))),
            current_priority=Priority(str(current_priority)) if current_priority else None,
            needs_info=bool(value.get("needs_info", False)),
            is_ready=bool(value.get("is_ready", False)),
            age_days=max(0, int(value.get("age_days", 0))),
        )


@dataclass(frozen=True)
class ScoreStep:
    name: str
    operation: str
    value: Decimal
    score_before: Decimal
    score_after: Decimal


@dataclass(frozen=True)
class ScoreResult:
    score: Decimal
    priority: Priority
    steps: tuple[ScoreStep, ...]


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)
