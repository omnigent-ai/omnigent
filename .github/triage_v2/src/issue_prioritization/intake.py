from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.classification import Classification
from issue_prioritization.duplicates import build_duplicate_comment, validate_duplicate_decision


@dataclass(frozen=True)
class IntakePlan:
    labels_add: tuple[str, ...]
    labels_remove: tuple[str, ...]
    assignee: str | None
    duplicate_decision: str
    duplicate_of: int | None
    similar_issues: tuple[int, ...]
    duplicate_confidence: float
    duplicate_comment: str
    close_as_duplicate: bool


def read_maintainers(path: str | Path) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in Path(path).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def plan_intake(
    issue: BronzeIssue,
    classification: Classification,
    areas: AreaCatalog,
    candidates: tuple[dict[str, object], ...],
    current_labels: tuple[str, ...],
    current_assignees: tuple[str, ...],
    maintainers: tuple[str, ...],
    assignee_load: Mapping[str, int],
    *,
    close_duplicates: bool,
    post_duplicate_comments: bool,
) -> IntakePlan:
    decision = validate_duplicate_decision(
        {
            "duplicate_decision": classification.duplicate_decision,
            "duplicate_of": classification.duplicate_of,
            "similar_issues": list(classification.similar_issues),
            "duplicate_confidence": classification.duplicate_confidence,
        },
        {"number": issue.number, "title": issue.title, "body": issue.body},
        list(candidates),
    )
    is_duplicate = decision["duplicate_decision"] == "duplicate"
    labels_add = ["triaged"]
    if classification.help_wanted and not is_duplicate:
        labels_add.append("help wanted")
    if is_duplicate:
        labels_add.append("duplicate")

    labels_add = [label for label in labels_add if label not in current_labels]
    labels_remove = ("needs-triage",) if "needs-triage" in current_labels else ()
    assignee = _choose_assignee(
        issue.author,
        classification.area_keys,
        areas,
        current_assignees,
        maintainers,
        assignee_load,
    )
    should_close = is_duplicate and close_duplicates
    comment = (
        build_duplicate_comment(
            decision,
            close_issue=should_close,
            reasoning=classification.duplicate_reasoning,
        )
        if post_duplicate_comments
        else ""
    )
    return IntakePlan(
        labels_add=tuple(labels_add),
        labels_remove=labels_remove,
        assignee=assignee,
        duplicate_decision=str(decision["duplicate_decision"]),
        duplicate_of=decision["duplicate_of"],
        similar_issues=tuple(decision["similar_issues"]),
        duplicate_confidence=float(decision["duplicate_confidence"]),
        duplicate_comment=comment,
        close_as_duplicate=should_close,
    )


def _choose_assignee(
    author: str,
    area_keys: tuple[str, ...],
    areas: AreaCatalog,
    current_assignees: tuple[str, ...],
    maintainers: tuple[str, ...],
    assignee_load: Mapping[str, int],
) -> str | None:
    if current_assignees:
        return None
    maintainers_by_key = {login.casefold(): login for login in maintainers}
    if author.casefold() in maintainers_by_key:
        return maintainers_by_key[author.casefold()]
    candidates = areas.owners_for(area_keys)
    if not candidates:
        return None
    normalized_load = {login.casefold(): count for login, count in assignee_load.items()}
    return min(candidates, key=lambda login: (normalized_load.get(login.casefold(), 0), login))
