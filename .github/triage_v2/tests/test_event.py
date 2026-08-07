from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from issue_prioritization.areas import Area, AreaCatalog
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.classification import Classification
from issue_prioritization.config import ScoringConfig
from issue_prioritization.domain import IssueType, Severity
from issue_prioritization.event import (
    prioritize_issue,
    target_for_labels,
    write_event_artifacts,
    write_event_status,
)
from issue_prioritization.labels import LabelDefinition, LabelManifest
from issue_prioritization.pipeline import PipelineMode
from issue_prioritization.scoring import ScoreEngine


class FakeClassifier:
    def classify(self, issue):
        return Classification(
            issue_number=issue.number,
            issue_type=IssueType.BUG,
            severity=Severity.S1,
            area_keys=("db",),
            component_labels=("comp:db",),
            reasoning="Breaks session startup.",
            content_hash=issue.content_hash,
        )


def _issue(labels=()) -> BronzeIssue:
    return BronzeIssue(
        number=7,
        title="Session fails",
        body="Cannot start a session",
        url="https://github.com/omnigent-ai/omnigent/issues/7",
        author="community",
        labels=labels,
        created_at=datetime(2026, 8, 6, tzinfo=UTC),
        upvote_count=0,
        duplicate_count=0,
    )


def _areas() -> AreaCatalog:
    area = Area("db", "comp:db", Decimal("1.2"))
    return AreaCatalog({"db": area}, {"comp:db": (area,)})


def _manifest() -> LabelManifest:
    return LabelManifest(
        (
            LabelDefinition("severity:S1", "000000", ""),
            LabelDefinition("severity:S3", "000000", ""),
            LabelDefinition("comp:db", "000000", ""),
        )
    )


def test_event_grades_and_plans_labels_for_one_issue() -> None:
    run, classification, _, _ = prioritize_issue(
        _issue(),
        FakeClassifier(),
        ScoringConfig.default(),
        _areas(),
        _manifest(),
        "github-1",
        PipelineMode.APPLY,
    )

    assert classification.severity == Severity.S1
    assert run.ranked[0].result.score == Decimal("72.00")
    assert set(run.mutations[0].labels_add) == {
        "P1-high",
        "comp:db",
        "severity:S1",
    }


def test_event_preserves_existing_human_priority_and_severity() -> None:
    run, _, _, _ = prioritize_issue(
        _issue(("P3-low", "severity:S3")),
        FakeClassifier(),
        ScoringConfig.default(),
        _areas(),
        _manifest(),
        "github-2",
        PipelineMode.APPLY,
    )

    assert run.ranked[0].issue.severity == Severity.S3
    assert run.ranked[0].result.priority.value == "P3-low"
    assert run.mutations[0].labels_add == ("comp:db",)


def test_event_artifact_contains_classification_and_mutation(tmp_path) -> None:
    issue = _issue()
    config = ScoringConfig.default()
    run, classification, _, _ = prioritize_issue(
        issue,
        FakeClassifier(),
        config,
        _areas(),
        _manifest(),
        "github-3",
        PipelineMode.DRY_RUN,
    )

    write_event_artifacts(
        tmp_path,
        run,
        classification,
        config,
        "test-endpoint",
        "abc123",
        issue.labels,
    )

    payload = json.loads((tmp_path / "event.json").read_text())
    assert payload["status"] == "planned"
    assert payload["classification"]["type"] == "Bug"
    assert payload["classification"]["severity"] == "S1"
    assert payload["classification"]["reasoning"] == "Breaks session startup."
    assert payload["score"]["score"] == 72.0
    assert payload["mutation"]["target"]["priority"] == "P1-high"
    assert payload["model_endpoint"] == "test-endpoint"
    assert payload["source_revision"] == "abc123"
    assert {path.name for path in tmp_path.iterdir()} == {
        "config.json",
        "event.json",
        "mutations.json",
    }

    write_event_status(
        tmp_path,
        run,
        classification,
        "test-endpoint",
        "abc123",
        issue.labels,
        status="apply_unknown",
    )
    assert json.loads((tmp_path / "event.json").read_text())["status"] == "apply_unknown"


def test_event_recomputes_priority_from_a_late_human_severity() -> None:
    issue = _issue()
    config = ScoringConfig.default()
    areas = _areas()
    run, classification, planner, _ = prioritize_issue(
        issue,
        FakeClassifier(),
        config,
        areas,
        _manifest(),
        "github-4",
        PipelineMode.APPLY,
    )

    target = target_for_labels(
        issue,
        classification,
        run.scored_at,
        ("severity:S3",),
        planner,
        None,
        ScoreEngine(config, areas),
    )

    assert target.severity == "severity:S3"
    assert target.priority == "P3-low"
