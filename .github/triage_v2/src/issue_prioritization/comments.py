from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal

from issue_prioritization.artifacts import RankedIssue
from issue_prioritization.domain import (
    EvidenceKind,
    InformationStatus,
    MissingInformation,
    Priority,
)
from issue_prioritization.mutations import MutationPlan

COMMENT_MARKER = "omnigent-issue-prioritization-v2"
NEEDS_INFO_DAYS = 7
_SPACE = re.compile(r"\s+")
_MISSING_INFORMATION_TEXT = {
    MissingInformation.TRIGGER: "the exact action, input, or sequence that triggers the problem",
    MissingInformation.EXPECTED_BEHAVIOR: "what you expected to happen",
    MissingInformation.OBSERVED_BEHAVIOR: "what happened instead, including the exact error",
    MissingInformation.VERSION_OR_ENVIRONMENT: (
        "the Omnigent version and relevant environment details"
    ),
    MissingInformation.DIAGNOSTIC_EVIDENCE: (
        "logs, screenshots, or session IDs that show the failure"
    ),
}
_EVIDENCE_TEXT = {
    EvidenceKind.DIRECT_STEPS: "direct reproduction steps",
    EvidenceKind.OBSERVED_INTERMITTENT: "a concrete intermittent observation",
    EvidenceKind.CONTROLLED_TEST: "a controlled test",
    EvidenceKind.DIAGNOSTIC_EVIDENCE: "diagnostic evidence",
    EvidenceKind.CODE_ANALYSIS: "concrete code-path analysis",
}


def build_triage_comment(
    item: RankedIssue,
    plan: MutationPlan,
    labels_after: tuple[str, ...],
    evaluated_at: datetime | None = None,
) -> str:
    needs_info = item.issue.information_status == InformationStatus.NEEDS_INFO and not any(
        value.startswith("needs_info_") for value in plan.blocked
    )
    deadline = (
        evaluated_at + timedelta(days=NEEDS_INFO_DAYS) if needs_info and evaluated_at else None
    )
    metadata = {
        "schema_version": 2,
        "base_score": float(_base_score(item)),
        "content_hash": item.issue.classification_content_hash,
        "information_status": item.issue.information_status.value,
        "needs_info_deadline": deadline.date().isoformat() if deadline else None,
    }
    marker = f"<!-- {COMMENT_MARKER} {json.dumps(metadata, separators=(',', ':'))} -->"
    priority_lines = _priority_lines(item, plan, labels_after)
    reasoning = _safe_reasoning(item.issue.classification_reasoning)
    information_lines = _information_lines(item, deadline)
    return "\n".join(
        (
            marker,
            "🤖 **Automated triage**",
            "",
            f"- **Bot assessment:** {item.issue.impact.label} impact",
            *priority_lines,
            *information_lines,
            f"- **Why:** {reasoning}",
            "",
            "This automated assessment uses the issue content and repository signals. "
            "Maintainers can override the priority label.",
        )
    )


def preserve_needs_info_deadline(body: str, existing_body: str) -> str:
    metadata = _comment_metadata(body)
    existing_metadata = _comment_metadata(existing_body)
    if not metadata or not existing_metadata:
        return body
    if (
        metadata.get("information_status") != InformationStatus.NEEDS_INFO
        or existing_metadata.get("information_status") != InformationStatus.NEEDS_INFO
        or not existing_metadata.get("needs_info_deadline")
    ):
        return body

    new_deadline = str(metadata.get("needs_info_deadline") or "")
    existing_deadline = str(existing_metadata["needs_info_deadline"])
    metadata["needs_info_deadline"] = existing_deadline
    lines = body.splitlines()
    lines[0] = f"<!-- {COMMENT_MARKER} {json.dumps(metadata, separators=(',', ':'))} -->"
    rendered = "\n".join(lines)
    if new_deadline:
        rendered = rendered.replace(f"**{new_deadline}**", f"**{existing_deadline}**", 1)
    return rendered


def _comment_metadata(body: str) -> dict[str, object] | None:
    first_line = body.splitlines()[0] if body else ""
    prefix = f"<!-- {COMMENT_MARKER} "
    if not first_line.startswith(prefix) or not first_line.endswith(" -->"):
        return None
    try:
        value = json.loads(first_line[len(prefix) : -4])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _information_lines(item: RankedIssue, deadline: datetime | None) -> tuple[str, ...]:
    issue = item.issue
    if issue.information_status == InformationStatus.NEEDS_INFO and deadline:
        requests = tuple(
            f"  - [ ] {_MISSING_INFORMATION_TEXT[value]}" for value in issue.missing_information
        )
        return (
            "- **Information status:** More information needed",
            "",
            "Please add the following details so this bug can be investigated:",
            *requests,
            "",
            f"Please update the issue by **{deadline.date().isoformat()}** "
            f"(within {NEEDS_INFO_DAYS} "
            "days). A comment or body edit will trigger another review; if the report is still "
            "missing this information after the deadline, it may be closed automatically.",
            "",
        )
    if evidence := _EVIDENCE_TEXT.get(issue.evidence_kind):
        return (f"- **Evidence:** {evidence}",)
    return ()


def _base_score(item: RankedIssue) -> Decimal:
    return next(
        (step.score_after for step in item.result.steps if step.name == "impact"),
        item.result.score,
    )


def _priority_lines(
    item: RankedIssue,
    plan: MutationPlan,
    labels_after: tuple[str, ...],
) -> tuple[str, ...]:
    priorities = [priority.value for priority in Priority if priority.value in labels_after]
    proposed = item.result.priority.value
    if "priority_label_conflict" in plan.blocked:
        return (
            "- **Priority:** Existing priority labels conflict and were preserved",
            f"- **Automated recommendation:** `{proposed}`",
        )
    if "priority_human_override" in plan.blocked:
        effective = f"`{priorities[0]}`" if len(priorities) == 1 else "None"
        return (
            f"- **Priority:** {effective} (human override retained)",
            f"- **Automated recommendation:** `{proposed}`",
        )
    return (f"- **Priority:** `{proposed}`",)


def _safe_reasoning(value: str) -> str:
    text = _SPACE.sub(" ", value).strip() or "No additional rationale was provided."
    return text[:500].replace("@", "@\u200b").replace("<", "&lt;").replace(">", "&gt;")
