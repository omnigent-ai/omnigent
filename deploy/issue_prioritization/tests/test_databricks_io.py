from __future__ import annotations

import json
from datetime import UTC, datetime

from issue_prioritization.config import ScoringConfig
from issue_prioritization.databricks_io import VolumeArtifactSink
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
