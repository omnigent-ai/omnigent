from __future__ import annotations

import json
import re
from decimal import Decimal

from issue_prioritization.artifacts import RankedIssue
from issue_prioritization.domain import Priority
from issue_prioritization.mutations import MutationPlan

COMMENT_MARKER = "omnigent-issue-prioritization-v2"
_SPACE = re.compile(r"\s+")


def build_triage_comment(
    item: RankedIssue,
    plan: MutationPlan,
    labels_after: tuple[str, ...],
) -> str:
    metadata = {
        "schema_version": 1,
        "base_score": float(_base_score(item)),
    }
    marker = f"<!-- {COMMENT_MARKER} {json.dumps(metadata, separators=(',', ':'))} -->"
    priority_lines = _priority_lines(item, plan, labels_after)
    reasoning = _safe_reasoning(item.issue.classification_reasoning)
    return "\n".join(
        (
            marker,
            "🤖 **Automated triage**",
            "",
            f"- **Bot assessment:** {item.issue.impact.label} impact",
            *priority_lines,
            f"- **Why:** {reasoning}",
            "",
            "This automated assessment uses the issue content and repository signals. "
            "Maintainers can override the priority label.",
        )
    )


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
