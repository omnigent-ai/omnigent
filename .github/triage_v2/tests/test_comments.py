from __future__ import annotations

from decimal import Decimal

from issue_prioritization.artifacts import RankedIssue
from issue_prioritization.comments import build_triage_comment
from issue_prioritization.domain import Impact, Issue, IssueType, Priority, ScoreResult, ScoreStep
from issue_prioritization.mutations import BotState, MutationPlan, MutationTarget


def _ranked(current_priority: Priority | None = None) -> RankedIssue:
    issue = Issue(
        7,
        "Session fails",
        "https://github.com/org/repo/issues/7",
        IssueType.BUG,
        Impact.HIGH,
        classification_reasoning="Blocks @team session startup. <unsafe>",
        current_priority=current_priority,
    )
    result = ScoreResult(
        Decimal("73.25"),
        Priority.P1,
        (ScoreStep("impact", "set", Decimal("60"), Decimal(0), Decimal("60")),),
    )
    return RankedIssue(1, 1, issue, result)


def test_comment_exposes_judgment_and_hides_base_score() -> None:
    plan = MutationPlan(
        MutationTarget(7, "P1-high", ()),
        ("P1-high",),
        (),
        (),
        BotState(7, "P1-high", ()),
    )

    body = build_triage_comment(_ranked(), plan, ("P1-high",))

    assert '"base_score":60.0' in body.splitlines()[0]
    assert "Base score" not in body
    assert "**Bot assessment:** High impact" in body
    assert "**Impact:**" not in body
    assert "**Priority:** `P1-high`" in body
    assert "@\u200bteam" in body
    assert "&lt;unsafe&gt;" in body


def test_comment_distinguishes_human_priority_from_recommendation() -> None:
    plan = MutationPlan(
        MutationTarget(7, "P1-high", ()),
        (),
        (),
        ("priority_human_override",),
        BotState(7, None, ()),
    )

    body = build_triage_comment(_ranked(Priority.P2), plan, ("P2-medium",))

    assert "**Priority:** `P2-medium` (human override retained)" in body
    assert "**Automated recommendation:** `P1-high`" in body


def test_comment_respects_a_human_removed_priority() -> None:
    plan = MutationPlan(
        MutationTarget(7, "P1-high", ()),
        (),
        (),
        ("priority_human_override",),
        BotState(7, "P1-high", ()),
    )

    body = build_triage_comment(_ranked(), plan, ())

    assert "**Priority:** None (human override retained)" in body
    assert "**Automated recommendation:** `P1-high`" in body
