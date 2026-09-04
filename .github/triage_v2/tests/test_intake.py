from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from issue_prioritization.areas import Area, AreaCatalog
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.classification import Classification
from issue_prioritization.domain import Impact, IssueType
from issue_prioritization.intake import plan_intake


def _issue(author: str = "community") -> BronzeIssue:
    return BronzeIssue(
        20,
        "Runner reconnect crashes after network disconnect",
        "The runner drops its active session and cannot reconnect after the network returns.",
        "https://github.com/omnigent-ai/omnigent/issues/20",
        author,
        (),
        datetime(2026, 9, 1, tzinfo=UTC),
        0,
        0,
    )


def _areas() -> AreaCatalog:
    runner = Area(
        "runner",
        "comp:runner",
        Decimal("1.2"),
        owners=("runner-b", "runner-a"),
    )
    web = Area("web", "comp:web-ui", Decimal("1.0"), owners=("web-a",))
    return AreaCatalog(
        {runner.key: runner, web.key: web},
        {runner.label: (runner,), web.label: (web,)},
    )


def _classification(**changes) -> Classification:
    values = {
        "issue_number": 20,
        "issue_type": IssueType.BUG,
        "impact": Impact.HIGH,
        "area_keys": ("runner",),
        "component_labels": ("comp:runner",),
        "reasoning": "The runner cannot recover after a network interruption.",
        "content_hash": "hash",
    }
    values.update(changes)
    return Classification(**values)


def test_intake_routes_to_the_least_loaded_area_owner() -> None:
    plan = plan_intake(
        _issue(),
        _classification(help_wanted=True),
        _areas(),
        (),
        ("needs-triage",),
        (),
        ("maintainer",),
        {"runner-a": 3, "runner-b": 1, "web-a": 0},
        close_duplicates=False,
        post_duplicate_comments=False,
    )

    assert plan.labels_add == ("triaged", "help wanted")
    assert plan.labels_remove == ("needs-triage",)
    assert plan.assignee == "runner-b"


def test_intake_keeps_an_existing_assignee_and_assigns_maintainer_authors() -> None:
    existing = plan_intake(
        _issue(),
        _classification(),
        _areas(),
        (),
        (),
        ("human",),
        ("maintainer",),
        {},
        close_duplicates=False,
        post_duplicate_comments=False,
    )
    maintainer = plan_intake(
        _issue("MAINTAINER"),
        _classification(),
        _areas(),
        (),
        (),
        (),
        ("maintainer",),
        {},
        close_duplicates=False,
        post_duplicate_comments=False,
    )

    assert existing.assignee is None
    assert maintainer.assignee == "maintainer"


def test_intake_closes_only_a_two_signal_duplicate() -> None:
    candidate = {
        "number": 12,
        "title": _issue().title,
        "body": _issue().body,
        "state": "OPEN",
        "similarity": 1.0,
    }
    plan = plan_intake(
        _issue(),
        _classification(
            help_wanted=True,
            duplicate_decision="duplicate",
            duplicate_of=12,
            duplicate_confidence=0.99,
            duplicate_reasoning="Both reports describe the same reconnect failure.",
        ),
        _areas(),
        (candidate,),
        (),
        (),
        ("maintainer",),
        {},
        close_duplicates=True,
        post_duplicate_comments=True,
    )

    assert plan.duplicate_decision == "duplicate"
    assert plan.duplicate_of == 12
    assert plan.close_as_duplicate
    assert plan.labels_add == ("triaged", "duplicate")
    assert "I’m closing it" in plan.duplicate_comment


def test_intake_downgrades_a_lexically_weak_duplicate() -> None:
    plan = plan_intake(
        _issue(),
        _classification(
            duplicate_decision="duplicate",
            duplicate_of=12,
            duplicate_confidence=0.99,
        ),
        _areas(),
        (
            {
                "number": 12,
                "title": "Android navigation color",
                "body": "The Android tab bar should use the blue theme.",
                "state": "OPEN",
                "similarity": 0.01,
            },
        ),
        (),
        (),
        (),
        {},
        close_duplicates=True,
        post_duplicate_comments=True,
    )

    assert plan.duplicate_decision == "none"
    assert not plan.close_as_duplicate
    assert plan.duplicate_comment == ""
