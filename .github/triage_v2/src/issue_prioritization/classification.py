from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from string import Template
from typing import Protocol

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.domain import (
    EvidenceKind,
    Impact,
    InformationStatus,
    IssueType,
    MissingInformation,
    Priority,
)

_PRIORITY_LABELS = {priority.value for priority in Priority}
_TYPE_LABELS = {
    "bug": IssueType.BUG,
    "feature": IssueType.ENHANCEMENT,
    "enhancement": IssueType.ENHANCEMENT,
    "docs": IssueType.DOCUMENTATION,
    "documentation": IssueType.DOCUMENTATION,
}
_PROMPT_TEMPLATE = Template(
    files("issue_prioritization").joinpath("classification_prompt.txt").read_text()
)


@dataclass(frozen=True)
class IssueContent:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    author: str

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            {
                "title": self.title,
                "body": self.body,
                "labels": sorted(_classification_labels(self.labels)),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class Classification:
    issue_number: int
    issue_type: IssueType
    impact: Impact
    area_keys: tuple[str, ...]
    component_labels: tuple[str, ...]
    reasoning: str
    content_hash: str
    reported_type: IssueType | None = None
    evidence_kind: EvidenceKind = EvidenceKind.NONE
    information_status: InformationStatus = InformationStatus.NOT_APPLICABLE
    missing_information: tuple[MissingInformation, ...] = ()
    help_wanted: bool = False
    duplicate_decision: str = "none"
    duplicate_of: int | None = None
    similar_issues: tuple[int, ...] = ()
    duplicate_confidence: float = 0.0
    duplicate_reasoning: str = ""


class Classifier(Protocol):
    def classify(self, issue: IssueContent) -> Classification: ...


class PromptClassifier:
    def __init__(
        self,
        query: Callable[[str], str],
        areas: AreaCatalog,
        duplicate_candidates: tuple[dict[str, object], ...] = (),
    ) -> None:
        self.query = query
        self.areas = areas
        self.duplicate_candidates = duplicate_candidates

    def classify(self, issue: IssueContent) -> Classification:
        response = self.query(build_prompt(issue, self.areas, self.duplicate_candidates))
        value = _parse_json_object(response)
        area_keys = tuple(
            key for key in _string_list(value.get("area_keys")) if key in self.areas.by_key
        )
        component_labels = tuple(
            dict.fromkeys(self.areas.by_key[key].issue_label for key in area_keys)
        )
        issue_type = _issue_type(value.get("type"))
        evidence_kind, information_status, missing_information = _information_assessment(
            issue_type, value
        )
        return Classification(
            issue_number=issue.number,
            issue_type=issue_type,
            impact=Impact.parse(value.get("impact", value.get("severity"))),
            area_keys=area_keys,
            component_labels=component_labels,
            reasoning=str(value.get("reasoning", "")),
            content_hash=issue.content_hash,
            reported_type=reported_issue_type(issue.labels),
            evidence_kind=evidence_kind,
            information_status=information_status,
            missing_information=missing_information,
            help_wanted=value.get("help_wanted") is True,
            duplicate_decision=str(value.get("duplicate_decision") or "none"),
            duplicate_of=_optional_int(value.get("duplicate_of")),
            similar_issues=tuple(_int_list(value.get("similar_issues"))),
            duplicate_confidence=_confidence(value.get("duplicate_confidence")),
            duplicate_reasoning=str(value.get("duplicate_reasoning") or ""),
        )


def build_prompt(
    issue: IssueContent,
    areas: AreaCatalog,
    duplicate_candidates: tuple[dict[str, object], ...] = (),
) -> str:
    area_lines = [
        f"- {area.key}: label={area.issue_label}. {area.definition}"
        for area in sorted(areas.by_key.values(), key=lambda item: item.key)
    ]
    return _PROMPT_TEMPLATE.substitute(
        allowed_areas="\n".join(area_lines),
        issue_number=issue.number,
        title=issue.title,
        labels=", ".join(issue.labels) if issue.labels else "none",
        author=issue.author,
        body=issue.body[:12000],
        duplicate_candidates=(
            json.dumps(duplicate_candidates, ensure_ascii=False, indent=2)
            if duplicate_candidates
            else "None. This is a reclassification; return duplicate_decision=none."
        ),
    )


def _parse_json_object(value: str) -> Mapping[str, object]:
    cleaned = value.replace("```json", "").replace("```", "").strip()
    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(cleaned, index)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping):
            return parsed
    raise ValueError("classifier did not return a JSON object")


def _issue_type(value: object) -> IssueType:
    return IssueType.parse(value)


def reported_issue_type(labels: tuple[str, ...]) -> IssueType | None:
    types = {_TYPE_LABELS[label.casefold()] for label in labels if label.casefold() in _TYPE_LABELS}
    return next(iter(types)) if len(types) == 1 else None


def _information_assessment(
    issue_type: IssueType,
    value: Mapping[str, object],
) -> tuple[EvidenceKind, InformationStatus, tuple[MissingInformation, ...]]:
    if issue_type != IssueType.BUG:
        return EvidenceKind.NONE, InformationStatus.NOT_APPLICABLE, ()

    evidence_kind = EvidenceKind.parse(value.get("evidence_kind"))
    information_status = InformationStatus.parse(value.get("information_status"))
    missing_information = tuple(
        dict.fromkeys(
            MissingInformation.parse(item)
            for item in _string_list(value.get("missing_information"))
        )
    )
    if information_status == InformationStatus.NOT_APPLICABLE:
        raise ValueError("bug information status cannot be not_applicable")
    if information_status == InformationStatus.SUFFICIENT and evidence_kind == EvidenceKind.NONE:
        raise ValueError("a sufficient bug report must identify usable evidence")
    if information_status == InformationStatus.NEEDS_INFO and not missing_information:
        raise ValueError("a needs-info bug report must identify missing information")
    return evidence_kind, information_status, missing_information


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, int) and not isinstance(item, bool)]


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return min(1.0, max(0.0, float(value)))


def _classification_labels(labels: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        label
        for label in labels
        if label not in _PRIORITY_LABELS
        and label.casefold() not in _TYPE_LABELS
        and label.casefold() != "needs-info"
        and not label.startswith("severity:")
        and not label.startswith("comp:")
    )
