from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from issue_prioritization.areas import Area, AreaCatalog
from issue_prioritization.config import ModuleConfig, ScoringConfig
from issue_prioritization.domain import Issue, IssueType, Priority, Severity
from issue_prioritization.scoring import ScoreEngine


def _catalog() -> AreaCatalog:
    areas = {
        "harness-claude": Area("harness-claude", "comp:harnesses", Decimal("1.4")),
        "harness-kimi": Area("harness-kimi", "comp:harnesses", Decimal("0.9")),
        "db": Area("db", "comp:server", Decimal("1.2")),
    }
    return AreaCatalog(
        by_key=areas,
        by_label={
            "comp:harnesses": (areas["harness-claude"], areas["harness-kimi"]),
            "comp:server": (areas["db"],),
        },
    )


def _issue(**changes: object) -> Issue:
    issue = Issue(
        number=1,
        title="Harness fails",
        url="https://github.com/omnigent-ai/omnigent/issues/1",
        issue_type=IssueType.BUG,
        severity=Severity.S1,
        area_keys=("harness-claude",),
    )
    return replace(issue, **changes)


def test_tier_one_s1_bug_stays_p1() -> None:
    result = ScoreEngine(ScoringConfig.default(), _catalog()).score(_issue())

    assert result.score == Decimal("84.00")
    assert result.priority == Priority.P1


def test_low_weight_s1_bug_falls_to_p2() -> None:
    result = ScoreEngine(ScoringConfig.default(), _catalog()).score(
        _issue(area_keys=("harness-kimi",))
    )

    assert result.score == Decimal("54.00")
    assert result.priority == Priority.P2


def test_duplicate_reach_is_capped() -> None:
    default = ScoringConfig.default()
    modules = dict(default.modules)
    modules["duplicates"] = ModuleConfig(True, modules["duplicates"].values)
    enabled = replace(default, modules=modules)

    result = ScoreEngine(enabled, _catalog()).score(
        _issue(severity=Severity.S2, duplicate_count=100)
    )

    assert result.score == Decimal("63.00")
    assert result.priority == Priority.P1


def test_needs_info_has_no_score() -> None:
    result = ScoreEngine(ScoringConfig.default(), _catalog()).score(_issue(needs_info=True))

    assert result.score == Decimal("0.00")
    assert result.priority == Priority.P3


def test_optional_modules_are_disabled_by_default() -> None:
    issue = _issue(is_ready=True, age_days=10)
    default = ScoringConfig.default()

    result = ScoreEngine(default, _catalog()).score(issue)

    assert result.score == Decimal("84.00")
    assert [step.name for step in result.steps] == [
        "severity",
        "component",
        "demand",
    ]


def test_optional_modules_can_be_enabled_independently() -> None:
    default = ScoringConfig.default()
    modules = dict(default.modules)
    modules["readiness"] = ModuleConfig(True, modules["readiness"].values)
    enabled = replace(default, modules=modules)

    result = ScoreEngine(enabled, _catalog()).score(_issue(is_ready=True, age_days=10))

    assert result.score == Decimal("92.40")
    assert "readiness" in [step.name for step in result.steps]
    assert "age" not in [step.name for step in result.steps]


def test_demand_is_linear_and_type_independent() -> None:
    engine = ScoreEngine(ScoringConfig.default(), _catalog())

    bug = engine.score(_issue(upvote_count=6))
    feature = engine.score(_issue(issue_type=IssueType.ENHANCEMENT, upvote_count=6))

    assert bug.score == Decimal("91.50")
    assert feature.score == bug.score


def test_demand_is_capped() -> None:
    result = ScoreEngine(ScoringConfig.default(), _catalog()).score(
        _issue(
            issue_type=IssueType.ENHANCEMENT,
            severity=Severity.S2,
            area_keys=("harness-kimi",),
            upvote_count=1000,
        )
    )

    assert result.score == Decimal("42.00")
    assert result.priority == Priority.P2


def test_linear_aligned_type_labels_are_normalized() -> None:
    feature = Issue.from_mapping(
        {
            "number": 1,
            "type": "Feature",
            "severity": "S2",
        }
    )
    docs = Issue.from_mapping(
        {
            "number": 2,
            "type": "Docs",
            "severity": "S3",
        }
    )

    assert feature.issue_type == IssueType.ENHANCEMENT
    assert docs.issue_type == IssueType.DOCUMENTATION
