from __future__ import annotations

from datetime import UTC, datetime

import pytest

from issue_prioritization.github import (
    GitHubClient,
    GitHubLegacyPriorityOwnership,
    GitHubMutationSink,
)
from issue_prioritization.labels import LabelDefinition, LabelManifest
from issue_prioritization.mutations import (
    BotState,
    MutationPlan,
    MutationPlanner,
    MutationTarget,
)
from issue_prioritization.pipeline import PipelineMode, PipelineRun


class FakeStates:
    def __init__(self, values):
        self.values = values
        self.updated = []

    def load(self):
        return self.values

    def upsert(self, states):
        self.updated.extend(states)


class FakeClient:
    def __init__(self):
        self.synced = False
        self.labels = ("P2-medium", "severity:S2", "comp:server")
        self.applied = []

    def sync_missing_labels(self, manifest):
        self.synced = True

    def issue_labels(self, issue_number):
        return self.labels

    def apply_labels(self, issue_number, labels_add, labels_remove):
        self.applied.append((issue_number, labels_add, labels_remove))


def _manifest() -> LabelManifest:
    return LabelManifest(
        labels=(
            LabelDefinition("severity:S1", "000000", ""),
            LabelDefinition("severity:S2", "000000", ""),
            LabelDefinition("comp:db", "000000", ""),
            LabelDefinition("comp:server", "000000", ""),
        )
    )


def test_apply_rechecks_live_labels_before_writing() -> None:
    state = BotState(1, "P2-medium", "severity:S2", ("comp:server",))
    states = FakeStates({1: state})
    manifest = _manifest()
    planner = MutationPlanner(manifest, states)
    target = MutationTarget(1, "P1-high", "severity:S1", ("comp:db",))
    proposed = MutationPlan(target, (), (), (), state)
    run = PipelineRun("run", PipelineMode.APPLY, datetime.now(UTC), (), 0, (proposed,))
    client = FakeClient()

    GitHubMutationSink(client, manifest, planner, states).apply(run)

    assert client.synced
    assert client.applied == [
        (
            1,
            ("P1-high", "comp:db", "severity:S1"),
            ("P2-medium", "comp:server", "severity:S2"),
        )
    ]
    assert states.updated[0].priority == "P1-high"


def test_apply_preserves_human_priority_changed_after_dry_run() -> None:
    state = BotState(1, "P2-medium", "severity:S2", ("comp:server",))
    states = FakeStates({1: state})
    manifest = _manifest()
    planner = MutationPlanner(manifest, states)
    target = MutationTarget(1, "P1-high", "severity:S2", ("comp:server",))
    proposed = MutationPlan(target, (), (), (), state)
    run = PipelineRun("run", PipelineMode.APPLY, datetime.now(UTC), (), 0, (proposed,))
    client = FakeClient()
    client.labels = ("P3-low", "severity:S2", "comp:server")

    GitHubMutationSink(client, manifest, planner, states).apply(run)

    assert client.applied == []
    assert states.updated == []


def test_apply_checkpoints_successful_writes_after_a_later_failure() -> None:
    first = BotState(1, "P2-medium", "severity:S2", ("comp:server",))
    second = BotState(2, "P2-medium", "severity:S2", ("comp:server",))
    states = FakeStates({1: first, 2: second})
    manifest = _manifest()
    planner = MutationPlanner(manifest, states)
    targets = (
        MutationPlan(
            MutationTarget(1, "P1-high", "severity:S1", ("comp:db",)),
            (),
            (),
            (),
            first,
        ),
        MutationPlan(
            MutationTarget(2, "P1-high", "severity:S1", ("comp:db",)),
            (),
            (),
            (),
            second,
        ),
    )
    run = PipelineRun("run", PipelineMode.APPLY, datetime.now(UTC), (), 0, targets)

    class FailingClient(FakeClient):
        def apply_labels(self, issue_number, labels_add, labels_remove):
            if issue_number == 2:
                raise RuntimeError("GitHub unavailable")
            super().apply_labels(issue_number, labels_add, labels_remove)

    with pytest.raises(RuntimeError, match="GitHub unavailable"):
        GitHubMutationSink(FailingClient(), manifest, planner, states).apply(run)

    assert [state.issue_number for state in states.updated] == [1]


def test_legacy_priority_uses_the_latest_label_actor() -> None:
    events = [
        {
            "id": 1,
            "event": "labeled",
            "label": {"name": "P2-medium"},
            "actor": {"login": "github-actions[bot]"},
        },
        {
            "id": 3,
            "event": "labeled",
            "label": {"name": "P2-medium"},
            "actor": {"login": "maintainer"},
        },
        {
            "id": 2,
            "event": "unlabeled",
            "label": {"name": "P2-medium"},
            "actor": {"login": "maintainer"},
        },
    ]
    client = GitHubClient("token", "org/repo", lambda method, path, payload: events)

    actor = client.priority_label_actor(1, "P2-medium")

    assert actor == "maintainer"
    assert not GitHubLegacyPriorityOwnership(
        client,
        {"github-actions[bot]"},
    ).is_bot_owned(1, "P2-medium")
