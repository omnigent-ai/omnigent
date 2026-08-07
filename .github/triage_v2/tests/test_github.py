from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from issue_prioritization.artifacts import RankedIssue
from issue_prioritization.domain import Impact, Issue, IssueType, Priority, ScoreResult, ScoreStep
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
        self.comments = []

    def sync_missing_labels(self, manifest):
        self.synced = True

    def issue_labels(self, issue_number):
        return self.labels

    def apply_labels(self, issue_number, labels_add, labels_remove):
        self.applied.append((issue_number, labels_add, labels_remove))

    def upsert_issue_comment(self, issue_number, body):
        self.comments.append((issue_number, body))
        return 42


def _manifest() -> LabelManifest:
    return LabelManifest(
        labels=(
            LabelDefinition("comp:db", "000000", ""),
            LabelDefinition("comp:server", "000000", ""),
        )
    )


def test_apply_rechecks_live_labels_before_writing() -> None:
    state = BotState(1, "P2-medium", ("comp:server",))
    states = FakeStates({1: state})
    manifest = _manifest()
    planner = MutationPlanner(manifest, states)
    target = MutationTarget(1, "P1-high", ("comp:db",))
    proposed = MutationPlan(target, (), (), (), state)
    run = PipelineRun("run", PipelineMode.APPLY, datetime.now(UTC), (), 0, (proposed,))
    client = FakeClient()

    GitHubMutationSink(client, manifest, planner, states).apply(run)

    assert client.synced
    assert client.applied == [
        (
            1,
            ("P1-high", "comp:db"),
            ("P2-medium", "comp:server", "severity:S2"),
        )
    ]
    assert states.updated[0].priority == "P1-high"


def test_apply_posts_the_ranked_bot_judgment() -> None:
    states = FakeStates({})
    manifest = _manifest()
    planner = MutationPlanner(manifest, states)
    target = MutationTarget(1, "P1-high", ("comp:db",))
    proposed = MutationPlan(target, (), (), (), BotState(1, None, ()))
    issue = Issue(
        1,
        "Session fails",
        "https://github.com/org/repo/issues/1",
        IssueType.BUG,
        Impact.HIGH,
        classification_reasoning="Blocks session startup.",
    )
    ranked = RankedIssue(
        1,
        1,
        issue,
        ScoreResult(
            Decimal("60"),
            Priority.P1,
            (ScoreStep("impact", "set", Decimal("60"), Decimal(0), Decimal("60")),),
        ),
    )
    run = PipelineRun(
        "run",
        PipelineMode.APPLY,
        datetime.now(UTC),
        (ranked,),
        0,
        (proposed,),
    )
    client = FakeClient()
    client.labels = ("severity:S2",)

    GitHubMutationSink(client, manifest, planner, states).apply(run)

    assert client.applied == [(1, ("P1-high", "comp:db"), ("severity:S2",))]
    assert len(client.comments) == 1
    assert "**Bot assessment:** High impact" in client.comments[0][1]


def test_apply_preserves_human_priority_changed_after_dry_run() -> None:
    state = BotState(1, "P2-medium", ("comp:server",))
    states = FakeStates({1: state})
    manifest = _manifest()
    planner = MutationPlanner(manifest, states)
    target = MutationTarget(1, "P1-high", ("comp:server",))
    proposed = MutationPlan(target, (), (), (), state)
    run = PipelineRun("run", PipelineMode.APPLY, datetime.now(UTC), (), 0, (proposed,))
    client = FakeClient()
    client.labels = ("P3-low", "severity:S2", "comp:server")

    GitHubMutationSink(client, manifest, planner, states).apply(run)

    assert client.applied == [(1, (), ("severity:S2",))]
    assert states.updated == []


def test_apply_can_recompute_target_from_live_labels() -> None:
    states = FakeStates({})
    manifest = _manifest()
    planner = MutationPlanner(manifest, states)
    proposed = MutationPlan(
        MutationTarget(1, "P1-high", ("comp:db",)),
        (),
        (),
        (),
        BotState(1, None, ()),
    )
    run = PipelineRun("run", PipelineMode.APPLY, datetime.now(UTC), (), 0, (proposed,))
    client = FakeClient()
    client.labels = ("severity:S3",)

    plans = GitHubMutationSink(
        client,
        manifest,
        planner,
        states,
        target_resolver=lambda target, labels, state: MutationTarget(
            target.issue_number,
            "P3-low",
            target.components,
        ),
    ).apply_with_plans(run)

    assert plans[0].target.priority == "P3-low"
    assert client.applied == [(1, ("P3-low", "comp:db"), ("severity:S3",))]


def test_apply_preserves_human_label_removals_after_dry_run() -> None:
    state = BotState(1, "P2-medium", ("comp:server",))
    states = FakeStates({1: state})
    manifest = _manifest()
    planner = MutationPlanner(manifest, states)
    target = MutationTarget(1, "P2-medium", ("comp:server",))
    proposed = MutationPlan(target, (), (), (), state)
    run = PipelineRun("run", PipelineMode.APPLY, datetime.now(UTC), (), 0, (proposed,))
    client = FakeClient()
    client.labels = ()

    GitHubMutationSink(client, manifest, planner, states).apply(run)

    assert client.applied == []
    assert states.updated == []


def test_apply_checkpoints_successful_writes_after_a_later_failure() -> None:
    first = BotState(1, "P2-medium", ("comp:server",))
    second = BotState(2, "P2-medium", ("comp:server",))
    states = FakeStates({1: first, 2: second})
    manifest = _manifest()
    planner = MutationPlanner(manifest, states)
    targets = (
        MutationPlan(
            MutationTarget(1, "P1-high", ("comp:db",)),
            (),
            (),
            (),
            first,
        ),
        MutationPlan(
            MutationTarget(2, "P1-high", ("comp:db",)),
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


def test_client_loads_a_live_open_issue() -> None:
    payload = {
        "number": 7,
        "title": "Session fails",
        "body": "Cannot start a session",
        "html_url": "https://github.com/org/repo/issues/7",
        "user": {"login": "community"},
        "labels": [{"name": "bug"}],
        "created_at": "2026-08-06T00:00:00Z",
        "reactions": {"+1": 3},
        "state": "open",
    }
    client = GitHubClient("token", "org/repo", lambda method, path, body: payload)

    issue = client.open_issue(7)

    assert issue is not None
    assert issue.number == 7
    assert issue.author == "community"
    assert issue.labels == ("bug",)
    assert issue.upvote_count == 3


def test_client_ignores_closed_issues_and_pull_requests() -> None:
    payload = {"state": "closed"}
    client = GitHubClient("token", "org/repo", lambda method, path, body: payload)
    assert client.open_issue(7) is None

    payload = {"state": "open", "pull_request": {}}
    assert client.open_issue(7) is None


def test_client_strips_token_whitespace() -> None:
    client = GitHubClient(" token\n", "org/repo", lambda method, path, body: None)

    assert client.token == "token"


@pytest.mark.parametrize("author_type", ("Bot", "User"))
def test_client_creates_and_updates_one_marker_comment(author_type: str) -> None:
    calls = []
    comments = []

    def transport(method, path, payload):
        calls.append((method, path, payload))
        if method == "GET":
            return comments
        if method == "POST":
            comments.append({"id": 42, "body": payload["body"], "user": {"type": author_type}})
            return comments[0]
        if method == "PATCH":
            comments[0]["body"] = payload["body"]
            return comments[0]
        raise AssertionError(method)

    client = GitHubClient("token", "org/repo", transport)
    first = "<!-- omnigent-issue-prioritization-v2 {} -->\nFirst"
    second = "<!-- omnigent-issue-prioritization-v2 {} -->\nSecond"

    assert client.upsert_issue_comment(7, first) == 42
    assert client.upsert_issue_comment(7, first) == 42
    assert client.upsert_issue_comment(7, second) == 42

    assert [method for method, _, _ in calls].count("POST") == 1
    assert [method for method, _, _ in calls].count("PATCH") == 1
    assert comments == [{"id": 42, "body": second, "user": {"type": author_type}}]
