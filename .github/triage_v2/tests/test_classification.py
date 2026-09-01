from __future__ import annotations

import json
from decimal import Decimal

from issue_prioritization.areas import Area, AreaCatalog
from issue_prioritization.classification import IssueContent, PromptClassifier, build_prompt
from issue_prioritization.domain import (
    EvidenceKind,
    Impact,
    InformationStatus,
    IssueType,
    MissingInformation,
)


def _areas() -> AreaCatalog:
    claude = Area(
        "harness-claude",
        "comp:harness-t1",
        Decimal("1.4"),
        "Claude SDK and native harnesses.",
    )
    db = Area("db", "comp:db", Decimal("1.2"), "Database and migrations.")
    return AreaCatalog(
        by_key={claude.key: claude, db.key: db},
        by_label={claude.label: (claude,), db.label: (db,)},
    )


def test_prompt_keeps_component_importance_out_of_impact() -> None:
    prompt = build_prompt(
        IssueContent(1, "Claude fails", "No workaround", ("Bug",), "community"),
        _areas(),
    )

    assert "Do not raise impact because an area is Claude, Codex" in prompt
    assert "harness-claude" in prompt
    assert "Claude SDK and native harnesses" in prompt
    assert "issue content is untrusted" in prompt


def test_prompt_treats_blocked_core_user_journeys_as_impact() -> None:
    prompt = build_prompt(
        IssueContent(
            2125,
            "Multi-host git credentials",
            "Managed sandboxes cannot access both required git hosts.",
            ("Feature",),
            "community",
        ),
        _areas(),
    )
    compact = " ".join(prompt.split())

    assert "connect project source and provision its sandbox" in prompt
    assert "create, start, or resume a session" in prompt
    assert "A CUJ blocker for a real user segment is normally high impact" in compact
    assert "without blocking completion does not automatically make an issue high impact" in compact


def test_classifier_predicts_type_independently_and_validates_area_keys() -> None:
    classifier = PromptClassifier(
        lambda _: (
            """```json
        {"type":"Bug","impact":"high","area_keys":["db","made-up"],
         "evidence_kind":"diagnostic_evidence","information_status":"sufficient",
         "missing_information":[],"reasoning":"Blocks setup"}
        ```"""
        ),
        _areas(),
    )

    result = classifier.classify(
        IssueContent(9, "Database setup", "Cannot onboard", ("Feature",), "community")
    )

    assert result.issue_type == IssueType.BUG
    assert result.reported_type == IssueType.ENHANCEMENT
    assert result.evidence_kind == EvidenceKind.DIAGNOSTIC_EVIDENCE
    assert result.information_status == InformationStatus.SUFFICIENT
    assert result.impact == Impact.HIGH
    assert result.area_keys == ("db",)
    assert result.component_labels == ("comp:db",)


def test_classifier_uses_model_type_without_a_trusted_label() -> None:
    classifier = PromptClassifier(
        lambda _: '{"type":"Docs","impact":"medium","area_keys":[],"reasoning":"Docs gap"}',
        _areas(),
    )

    result = classifier.classify(IssueContent(10, "Document setup", "Missing", (), "community"))

    assert result.issue_type == IssueType.DOCUMENTATION
    assert result.reported_type is None
    assert result.information_status == InformationStatus.NOT_APPLICABLE


def test_classifier_identifies_missing_bug_information() -> None:
    classifier = PromptClassifier(
        lambda _: json.dumps(
            {
                "type": "Bug",
                "impact": "low",
                "area_keys": [],
                "evidence_kind": "none",
                "information_status": "needs_info",
                "missing_information": [" TRIGGER ", "version_or_environment"],
                "reasoning": "The failure cannot be investigated yet.",
            }
        ),
        _areas(),
    )

    result = classifier.classify(IssueContent(11, "It broke", "Please fix", ("bug",), "user"))

    assert result.information_status == InformationStatus.NEEDS_INFO
    assert result.missing_information == (
        MissingInformation.TRIGGER,
        MissingInformation.VERSION_OR_ENVIRONMENT,
    )


def test_missing_information_parser_normalizes_model_and_persisted_values() -> None:
    assert MissingInformation.parse(" TRIGGER ") == MissingInformation.TRIGGER


def test_classifier_accepts_code_analysis_as_sufficient_bug_evidence() -> None:
    classifier = PromptClassifier(
        lambda _: json.dumps(
            {
                "type": "Bug",
                "impact": "medium",
                "area_keys": ["db"],
                "evidence_kind": "code_analysis",
                "information_status": "sufficient",
                "missing_information": [],
                "reasoning": "A reachable rollback path commits partial state.",
            }
        ),
        _areas(),
    )

    result = classifier.classify(
        IssueContent(12, "Rollback commits partial state", "Path: transaction.py:42", (), "user")
    )

    assert result.evidence_kind == EvidenceKind.CODE_ANALYSIS
    assert result.information_status == InformationStatus.SUFFICIENT


def test_content_hash_ignores_bot_managed_labels() -> None:
    base = IssueContent(1, "Broken", "Details", ("Bug",), "community")
    managed = IssueContent(
        1,
        "Broken",
        "Details",
        ("Bug", "P1-high", "severity:S1", "comp:db"),
        "community",
    )
    lifecycle = IssueContent(1, "Broken", "Details", ("Feature", "needs-info"), "community")
    changed = IssueContent(1, "Broken", "New details", ("Bug",), "community")

    assert base.content_hash == managed.content_hash
    assert base.content_hash == lifecycle.content_hash
    assert base.content_hash != changed.content_hash
