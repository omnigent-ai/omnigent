from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from string import Template
from typing import Protocol

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.domain import Impact, IssueType, Priority

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


class Classifier(Protocol):
    def classify(self, issue: IssueContent) -> Classification: ...


class PromptClassifier:
    def __init__(
        self,
        query: Callable[[str], str],
        areas: AreaCatalog,
    ) -> None:
        self.query = query
        self.areas = areas

    def classify(self, issue: IssueContent) -> Classification:
        response = self.query(build_prompt(issue, self.areas))
        value = _parse_json_object(response)
        area_keys = tuple(
            key for key in _string_list(value.get("area_keys")) if key in self.areas.by_key
        )
        component_labels = tuple(
            dict.fromkeys(self.areas.by_key[key].issue_label for key in area_keys)
        )
        return Classification(
            issue_number=issue.number,
            issue_type=_labeled_issue_type(issue.labels) or _issue_type(value.get("type")),
            impact=Impact.parse(value.get("impact", value.get("severity"))),
            area_keys=area_keys,
            component_labels=component_labels,
            reasoning=str(value.get("reasoning", "")),
            content_hash=issue.content_hash,
        )


def build_prompt(issue: IssueContent, areas: AreaCatalog) -> str:
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


def _labeled_issue_type(labels: tuple[str, ...]) -> IssueType | None:
    types = {_TYPE_LABELS[label.casefold()] for label in labels if label.casefold() in _TYPE_LABELS}
    return next(iter(types)) if len(types) == 1 else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _classification_labels(labels: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        label
        for label in labels
        if label not in _PRIORITY_LABELS
        and not label.startswith("severity:")
        and not label.startswith("comp:")
    )
