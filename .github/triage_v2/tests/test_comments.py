from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from issue_prioritization.artifacts import RankedIssue
from issue_prioritization.comments import build_triage_comment, preserve_needs_info_deadline
from issue_prioritization.domain import (
    EvidenceKind,
    Impact,
    InformationStatus,
    Issue,
    IssueType,
    MissingInformation,
    Priority,
    ScoreResult,
    ScoreStep,
)
from issue_prioritization.mutations import BotState, MutationPlan, MutationTarget


def _ranked(
    current_priority: Priority | None = None,
    *,
    information_status: InformationStatus = InformationStatus.NOT_APPLICABLE,
    evidence_kind: EvidenceKind = EvidenceKind.NONE,
    missing_information: tuple[MissingInformation, ...] = (),
) -> RankedIssue:
    issue = Issue(
        7,
        "Session fails",
        "https://github.com/org/repo/issues/7",
        IssueType.BUG,
        Impact.HIGH,
        classification_reasoning="Blocks @team session startup. <unsafe>",
        classification_content_hash="content-v1",
        information_status=information_status,
        evidence_kind=evidence_kind,
        missing_information=missing_information,
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


def test_comment_requests_specific_information_with_seven_day_deadline() -> None:
    plan = MutationPlan(
        MutationTarget(7, "P1-high", (), "Bug", True),
        ("needs-info",),
        (),
        (),
        BotState(7, None, ()),
    )
    ranked = _ranked(
        information_status=InformationStatus.NEEDS_INFO,
        missing_information=(
            MissingInformation.TRIGGER,
            MissingInformation.VERSION_OR_ENVIRONMENT,
        ),
    )

    body = build_triage_comment(
        ranked,
        plan,
        ("needs-info",),
        datetime(2026, 8, 28, 12, tzinfo=UTC),
    )

    assert '"needs_info_deadline":"2026-09-04"' in body.splitlines()[0]
    assert "exact action, input, or sequence" in body
    assert "Omnigent version and relevant environment" in body
    assert "what you expected" not in body
    assert "within 7 days" in body


def test_comment_records_usable_code_analysis_without_requesting_steps() -> None:
    plan = MutationPlan(
        MutationTarget(7, "P1-high", (), "Bug", False),
        (),
        (),
        (),
        BotState(7, None, ()),
    )

    body = build_triage_comment(
        _ranked(
            information_status=InformationStatus.SUFFICIENT,
            evidence_kind=EvidenceKind.CODE_ANALYSIS,
        ),
        plan,
        (),
    )

    assert "**Evidence:** concrete code-path analysis" in body
    assert "More information needed" not in body


def test_continuous_needs_info_keeps_deadline_after_content_changes() -> None:
    plan = MutationPlan(
        MutationTarget(7, "P1-high", (), "Bug", True),
        ("needs-info",),
        (),
        (),
        BotState(7, None, ()),
    )
    ranked = _ranked(
        information_status=InformationStatus.NEEDS_INFO,
        missing_information=(MissingInformation.TRIGGER,),
    )
    original = build_triage_comment(
        ranked,
        plan,
        ("needs-info",),
        datetime(2026, 8, 28, tzinfo=UTC),
    )
    refreshed = build_triage_comment(
        ranked,
        plan,
        ("needs-info",),
        datetime(2026, 8, 30, tzinfo=UTC),
    )
    refreshed = refreshed.replace('"content_hash":"content-v1"', '"content_hash":"content-v2"')

    preserved = preserve_needs_info_deadline(refreshed, original)

    assert '"needs_info_deadline":"2026-09-04"' in preserved.splitlines()[0]
    assert '"content_hash":"content-v2"' in preserved.splitlines()[0]
    assert "**2026-09-04**" in preserved


def test_new_needs_info_transition_starts_a_new_deadline() -> None:
    sufficient_plan = MutationPlan(
        MutationTarget(7, "P1-high", (), "Bug", False),
        (),
        (),
        (),
        BotState(7, None, ()),
    )
    needs_info_plan = MutationPlan(
        MutationTarget(7, "P1-high", (), "Bug", True),
        ("needs-info",),
        (),
        (),
        BotState(7, None, ()),
    )
    original = build_triage_comment(
        _ranked(information_status=InformationStatus.SUFFICIENT),
        sufficient_plan,
        (),
        datetime(2026, 8, 28, tzinfo=UTC),
    )
    refreshed = build_triage_comment(
        _ranked(
            information_status=InformationStatus.NEEDS_INFO,
            missing_information=(MissingInformation.TRIGGER,),
        ),
        needs_info_plan,
        ("needs-info",),
        datetime(2026, 8, 30, tzinfo=UTC),
    )

    preserved = preserve_needs_info_deadline(refreshed, original)

    assert '"needs_info_deadline":"2026-09-06"' in preserved.splitlines()[0]
    assert "**2026-09-06**" in preserved
