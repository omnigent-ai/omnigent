from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from issue_prioritization import event
from issue_prioritization.areas import Area, AreaCatalog
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.classification import Classification
from issue_prioritization.config import ScoringConfig
from issue_prioritization.domain import Impact, IssueType
from issue_prioritization.event import (
    _apply_intake,
    prioritize_issue,
    target_for_labels,
    write_event_artifacts,
    write_event_status,
)
from issue_prioritization.intake import IntakePlan
from issue_prioritization.labels import LabelDefinition, LabelManifest
from issue_prioritization.pipeline import PipelineMode
from issue_prioritization.scoring import ScoreEngine


class FakeClassifier:
    def classify(self, issue):
        return Classification(
            issue_number=issue.number,
            issue_type=IssueType.BUG,
            impact=Impact.HIGH,
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
    return LabelManifest((LabelDefinition("comp:db", "000000", ""),))


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

    assert classification.impact == Impact.HIGH
    assert run.ranked[0].result.score == Decimal("72.00")
    assert set(run.mutations[0].labels_add) == {
        "Bug",
        "P1-high",
        "comp:db",
    }


def test_event_preserves_human_priority_and_retires_severity_label() -> None:
    run, _, _, _ = prioritize_issue(
        _issue(("P3-low", "severity:S3")),
        FakeClassifier(),
        ScoringConfig.default(),
        _areas(),
        _manifest(),
        "github-2",
        PipelineMode.APPLY,
    )

    assert run.ranked[0].issue.impact == Impact.HIGH
    assert run.ranked[0].result.priority.value == "P1-high"
    assert run.mutations[0].labels_add == ("Bug", "comp:db")
    assert run.mutations[0].labels_remove == ("severity:S3",)
    assert run.mutations[0].blocked == ("priority_human_override",)


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
    assert payload["schema_version"] == 2
    assert payload["classification"]["impact"] == "high"
    assert payload["classification"]["reasoning"] == "Breaks session startup."
    assert payload["classification"]["evidence_kind"] == "none"
    assert payload["classification"]["information_status"] == "not_applicable"
    assert payload["classification"]["missing_information"] == []
    assert payload["score"]["score"] == 72.0
    assert payload["mutation"]["target"]["priority"] == "P1-high"
    assert payload["mutation"]["target"]["issue_type"] == "Bug"
    assert payload["mutation"]["target"]["needs_info"] is False
    assert payload["model_endpoint"] == "test-endpoint"
    assert payload["source_revision"] == "abc123"
    assert "<!-- omnigent-issue-prioritization-v2" in payload["comment"]["body"]
    assert '"base_score":60.0' in payload["comment"]["body"]
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


def test_event_ignores_a_retired_severity_label_when_recomputing() -> None:
    issue = _issue()
    config = ScoringConfig.default()
    areas = _areas()
    run, classification, _, _ = prioritize_issue(
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
        ScoreEngine(config, areas),
    )

    assert target.priority == "P1-high"


def test_intake_assigns_before_duplicate_closure() -> None:
    events = []

    class Client:
        def apply_labels(self, issue_number, labels_add, labels_remove):
            events.append("labels")

        def comment_on_issue_once(self, issue_number, marker, body):
            events.append("comment")

        def issue_data(self, issue_number):
            return {"state": "open", "assignees": []}

        def assign_issue(self, issue_number, assignee):
            events.append("assign")

        def close_as_duplicate(self, issue_number, duplicate_of):
            events.append("close")

    plan = IntakePlan(
        ("triaged", "duplicate"),
        ("needs-triage",),
        "owner",
        "duplicate",
        3,
        (),
        0.99,
        "<!-- omnigent-duplicate-check -->\nClosing",
        True,
    )

    _apply_intake(Client(), 7, plan)

    assert events == ["labels", "comment", "assign", "close"]


@pytest.mark.parametrize("intake", [False, True])
def test_event_fetches_related_issues_for_intake_and_edits(monkeypatch, tmp_path, intake) -> None:
    issue = replace(_issue(), body="Cannot start a session; see #3.")
    captured = []

    class Client:
        def open_issue(self, number, *, full_author_history):
            assert number == issue.number
            assert full_author_history
            return issue

        def issue_corpus(self):
            return (
                {
                    "number": 3,
                    "title": "Session fails",
                    "body": "Cannot start a session",
                    "state": "closed",
                    "state_reason": "completed",
                },
            )

        def issue_data(self, number):
            assert intake, "Edits must not run intake actions"
            return {"state": "open", "labels": [], "assignees": []}

        def assignee_load(self):
            assert intake
            return {}

    def classifier(endpoint, areas, *, duplicate_candidates, review_bugs):
        captured.extend(duplicate_candidates)
        assert review_bugs
        return FakeClassifier()

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(event, "GitHubClient", lambda *_: Client())
    monkeypatch.setattr(event, "serving_endpoint_classifier", classifier)
    github_dir = Path(__file__).resolve().parents[2]
    argv = [
        "issue-priority-event",
        "--issue-number",
        "7",
        "--github-repo",
        "omnigent-ai/omnigent",
        "--model-endpoint",
        "test-endpoint",
        "--areas",
        str(github_dir / "areas.json"),
        "--label-manifest",
        str(github_dir / "issue-prioritization-labels.json"),
        "--output-dir",
        str(tmp_path),
        "--run-id",
        "test-event",
        "--close-duplicates",
        "--post-duplicate-comments",
    ]
    if intake:
        argv.extend(["--intake", "--maintainers", str(github_dir / "MAINTAINER")])
    monkeypatch.setattr("sys.argv", argv)

    event.main()

    assert [candidate["number"] for candidate in captured] == [3]
    payload = json.loads((tmp_path / "event.json").read_text())
    assert (payload["intake"] is not None) == intake
