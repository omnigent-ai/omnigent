from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from databricks.sdk.service.serving import ChatMessageRole

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.classification import IssueContent
from issue_prioritization.config import ScoringConfig
from issue_prioritization.databricks_io import (
    VolumeArtifactSink,
    latest_scores_view_sql,
)
from issue_prioritization.domain import IssueType
from issue_prioritization.model_serving import serving_endpoint_classifier
from issue_prioritization.mutations import BotState, MutationPlan, MutationTarget
from issue_prioritization.pipeline import PipelineMode, PipelineRun


def test_dry_run_artifact_contains_complete_mutation_plan(tmp_path) -> None:
    target = MutationTarget(7, "P1-high", "severity:S1", ("comp:db",))
    plan = MutationPlan(
        target=target,
        labels_add=("P1-high", "severity:S1", "comp:db"),
        labels_remove=("P2-medium",),
        blocked=(),
        next_state=BotState(7, "P1-high", "severity:S1", ("comp:db",)),
    )
    run = PipelineRun(
        "preview",
        PipelineMode.DRY_RUN,
        datetime.now(UTC),
        (),
        0,
        (plan,),
    )

    VolumeArtifactSink(str(tmp_path), ScoringConfig.default()).write(run)

    payload = json.loads((tmp_path / "preview" / "mutations.json").read_text())
    assert payload == [
        {
            "issue_number": 7,
            "target": {
                "priority": "P1-high",
                "severity": "severity:S1",
                "components": ["comp:db"],
            },
            "labels_add": ["P1-high", "severity:S1", "comp:db"],
            "labels_remove": ["P2-medium"],
            "blocked": [],
            "next_bot_state": {
                "priority": "P1-high",
                "severity": "severity:S1",
                "components": ["comp:db"],
            },
        }
    ]
    metadata = json.loads((tmp_path / "preview" / "run.json").read_text())
    assert metadata["mode"] == "dry_run"
    assert metadata["adopt_legacy_bot_priorities"] is False
    assert metadata["legacy_priorities_adopted"] == 0
    assert not (tmp_path / "preview" / ".run.json.tmp").exists()


def test_latest_scores_view_selects_one_complete_run() -> None:
    statement = latest_scores_view_sql(
        "main.team.issue_scores",
        "main.team.issue_scores_latest",
    )

    assert statement.startswith("CREATE OR REPLACE VIEW main.team.issue_scores_latest")
    assert "max_by(run_id, scored_at) FROM main.team.issue_scores" in statement


class FakeServingEndpoints:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = []

    def query(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        return self.response


def test_serving_classifier_uses_online_chat_endpoint() -> None:
    payload = json.dumps(
        {
            "type": "Bug",
            "severity": "S2",
            "area_keys": [],
            "reasoning": "Affects a real workflow.",
        }
    )
    serving = FakeServingEndpoints(
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=payload), text=None)]
        )
    )
    workspace = SimpleNamespace(serving_endpoints=serving)
    classifier = serving_endpoint_classifier("test-endpoint", AreaCatalog({}, {}), workspace)

    result = classifier.classify(IssueContent(7, "Broken flow", "It fails", (), "user"))

    assert result.issue_type == IssueType.BUG
    endpoint, request = serving.calls[0]
    assert endpoint == "test-endpoint"
    assert request["max_tokens"] == 2048
    assert request["messages"][0].role == ChatMessageRole.USER
    assert "Broken flow" in request["messages"][0].content


def test_serving_classifier_rejects_empty_response() -> None:
    serving = FakeServingEndpoints(SimpleNamespace(choices=[]))
    workspace = SimpleNamespace(serving_endpoints=serving)
    classifier = serving_endpoint_classifier("test-endpoint", AreaCatalog({}, {}), workspace)

    with pytest.raises(RuntimeError, match="no choices"):
        classifier.classify(IssueContent(7, "Broken", "", (), "user"))
