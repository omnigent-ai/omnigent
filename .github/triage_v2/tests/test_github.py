from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from urllib.error import HTTPError

import pytest

from issue_prioritization.artifacts import RankedIssue
from issue_prioritization.domain import Impact, Issue, IssueType, Priority, ScoreResult, ScoreStep
from issue_prioritization.github import (
    GitHubClient,
    GitHubLegacyPriorityOwnership,
    GitHubMutationSink,
    GitHubNotFound,
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

    def sync_issue_comment(self, issue_number, body, state=None):
        self.comments.append((issue_number, body))
        return 42, body, True


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


def test_apply_skips_an_issue_deleted_from_github() -> None:
    # The bronze snapshot lags GitHub: #2 was deleted/transferred since
    # ingestion, so its live re-check 404s. It must be skipped, not abort the
    # whole apply — #1 still gets written.
    first = BotState(1, "P2-medium", ("comp:server",))
    second = BotState(2, "P2-medium", ("comp:server",))
    states = FakeStates({1: first, 2: second})
    manifest = _manifest()
    planner = MutationPlanner(manifest, states)
    targets = (
        MutationPlan(MutationTarget(1, "P1-high", ("comp:db",)), (), (), (), first),
        MutationPlan(MutationTarget(2, "P1-high", ("comp:db",)), (), (), (), second),
    )
    run = PipelineRun("run", PipelineMode.APPLY, datetime.now(UTC), (), 0, targets)

    class DeletedIssueClient(FakeClient):
        def issue_labels(self, issue_number):
            if issue_number == 2:
                raise GitHubNotFound("GitHub API GET /issues/2 failed: 404 Not Found")
            return super().issue_labels(issue_number)

    plans = GitHubMutationSink(DeletedIssueClient(), manifest, planner, states).apply_with_plans(
        run
    )

    assert [plan.target.issue_number for plan in plans] == [1]
    assert [state.issue_number for state in states.updated] == [1]


def test_request_maps_404_to_not_found_and_keeps_other_errors_generic(monkeypatch) -> None:
    import io

    from issue_prioritization import github as github_module

    def raise_http(code):
        def _open(request, timeout=0):
            raise HTTPError(request.full_url, code, "boom", {}, io.BytesIO(b"{}"))

        return _open

    client = GitHubClient("token", "org/repo", max_attempts=1)

    monkeypatch.setattr(github_module, "urlopen", raise_http(404))
    with pytest.raises(GitHubNotFound):
        client.issue_data(6863)

    monkeypatch.setattr(github_module, "urlopen", raise_http(500))
    with pytest.raises(RuntimeError) as exc:
        client.issue_data(6863)
    assert not isinstance(exc.value, GitHubNotFound)


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

    def transport(method, path, body):
        return [] if "/comments" in path else payload

    client = GitHubClient("token", "org/repo", transport)

    issue = client.open_issue(7)

    assert issue is not None
    assert issue.number == 7
    assert issue.author == "community"
    assert issue.labels == ("bug",)
    assert issue.upvote_count == 3


def test_client_includes_only_author_follow_up_comments() -> None:
    payload = {
        "number": 7,
        "title": "Session fails",
        "body": "Original report",
        "html_url": "https://github.com/org/repo/issues/7",
        "user": {"login": "community"},
        "labels": [{"name": "Bug"}],
        "created_at": "2026-08-06T00:00:00Z",
        "state": "open",
    }
    comments = [
        {"user": {"login": "maintainer"}, "body": "Could you share logs?"},
        {"user": {"login": "community"}, "body": "The session ID is abc-123."},
    ]

    def transport(method, path, body):
        return comments if "/comments" in path else payload

    issue = GitHubClient("token", "org/repo", transport).open_issue(7)

    assert issue is not None
    assert "The session ID is abc-123." in issue.body
    assert "Could you share logs?" not in issue.body
    assert "Original report" in issue.body


def test_client_ignores_closed_issues_and_pull_requests() -> None:
    payload = {"state": "closed"}
    client = GitHubClient("token", "org/repo", lambda method, path, body: payload)
    assert client.open_issue(7) is None

    payload = {"state": "open", "pull_request": {}}
    assert client.open_issue(7) is None


def test_client_lists_and_closes_labeled_open_issues() -> None:
    calls = []
    issue = {"number": 7, "state": "open", "labels": [{"name": "needs-info"}]}

    def transport(method, path, body):
        calls.append((method, path, body))
        if path.startswith("/issues?"):
            return [issue]
        if method == "GET" and path == "/issues/7":
            return issue
        return {}

    client = GitHubClient("token", "org/repo", transport)

    assert client.open_issues_with_label("needs-info") == (issue,)
    client.comment_on_issue(7, "Closing")
    client.close_issue(7)

    assert ("POST", "/issues/7/comments", {"body": "Closing"}) in calls
    assert (
        "PATCH",
        "/issues/7",
        {"state": "closed", "state_reason": "not_planned"},
    ) in calls


def test_client_lists_corpus_counts_load_and_filters_pull_requests() -> None:
    calls = []

    def transport(method, path, body):
        calls.append((method, path, body))
        if "state=all" in path:
            return [
                {"number": 1, "assignees": []},
                {"number": 2, "pull_request": {}, "assignees": []},
            ]
        if "state=open" in path:
            return [
                {"number": 1, "assignees": [{"login": "owner"}]},
                {"number": 2, "pull_request": {}, "assignees": [{"login": "owner"}]},
            ]
        raise AssertionError(path)

    client = GitHubClient("token", "org/repo", transport)

    assert [issue["number"] for issue in client.issue_corpus()] == [1]
    assert client.assignee_load() == {"owner": 1}


def test_client_posts_a_duplicate_comment_only_once() -> None:
    comments = []

    def transport(method, path, body):
        if method == "GET":
            return comments
        comments.append({"id": 1, "body": body["body"]})
        return comments[-1]

    client = GitHubClient("token", "org/repo", transport)

    assert client.comment_on_issue_once(7, "<!-- marker -->", "<!-- marker -->\nFirst")
    assert not client.comment_on_issue_once(7, "<!-- marker -->", "<!-- marker -->\nSecond")
    assert len(comments) == 1


def test_client_assigns_and_closes_with_github_duplicate_metadata() -> None:
    calls = []

    def transport(method, path, body):
        calls.append((method, path, body))
        if method == "GET" and path == "/issues/7":
            return {"node_id": "issue-node"}
        if method == "GET" and path == "/issues/3":
            return {"node_id": "duplicate-node"}
        return {}

    client = GitHubClient("token", "org/repo", transport)
    client.assign_issue(7, "owner")
    client.close_as_duplicate(7, 3)

    assert ("POST", "/issues/7/assignees", {"assignees": ["owner"]}) in calls
    graphql = next(call for call in calls if call[1] == "/graphql")
    assert graphql[2]["variables"]["input"] == {
        "issueId": "issue-node",
        "stateReason": "DUPLICATE",
        "duplicateIssueId": "duplicate-node",
    }


def test_client_surfaces_graphql_duplicate_closure_errors() -> None:
    def transport(method, path, body):
        if method == "GET":
            return {"node_id": f"node-{path.rsplit('/', 1)[-1]}"}
        return {"errors": [{"message": "target is unavailable"}]}

    client = GitHubClient("token", "org/repo", transport)

    with pytest.raises(RuntimeError, match="target is unavailable"):
        client.close_as_duplicate(7, 3)


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


@pytest.mark.parametrize("needs_info", [False, True])
def test_apply_unchanged_rerun_has_no_writes_or_comment_reads(needs_info, capsys):
    from dataclasses import replace
    from datetime import timedelta

    from issue_prioritization.domain import InformationStatus

    calls = []
    labels = {"Bug", "P1-high", "comp:db"}
    if needs_info:
        labels.add("needs-info")
    comments = []

    def transport(method, path, payload):
        calls.append((method, path))
        if path.startswith("/labels?"):
            return [{"name": label.name} for label in _manifest().labels]
        if method == "GET" and path == "/issues/1":
            return {"labels": [{"name": label} for label in labels]}
        if method == "GET" and "/comments?" in path:
            return comments
        if method == "POST" and path == "/issues/1/comments":
            comments.append({"id": 42, "body": payload["body"]})
            return comments[-1]
        raise AssertionError((method, path))

    states = FakeStates({1: BotState(1, "P1-high", ("comp:db",))})
    planner = MutationPlanner(_manifest(), states)
    issue = Issue(
        1,
        "Session fails",
        "https://github.com/org/repo/issues/1",
        IssueType.BUG,
        Impact.HIGH,
        information_status=(
            InformationStatus.NEEDS_INFO if needs_info else InformationStatus.SUFFICIENT
        ),
    )
    item = RankedIssue(1, 1, issue, ScoreResult(Decimal(60), Priority.P1, ()))
    proposed = planner.plan_one(
        MutationTarget(1, "P1-high", ("comp:db",)), tuple(labels), states.values[1]
    )
    run = PipelineRun("run", PipelineMode.APPLY, datetime.now(UTC), (item,), 0, (proposed,))
    sink = GitHubMutationSink(
        GitHubClient("token", "org/repo", transport), _manifest(), planner, states
    )
    sink.apply(run)
    assert states.updated[-1].comment_id == 42
    assert len(comments) == 1
    calls.clear()

    sink.apply(replace(run, scored_at=run.scored_at + timedelta(days=1)))

    assert calls == [("GET", "/labels?per_page=100&page=1"), ("GET", "/issues/1")]
    assert "mutated=0, skipped_unchanged=1" in capsys.readouterr().out


@pytest.mark.parametrize("deleted", [False, True])
def test_cached_comment_updates_directly_and_recovers_from_deleted_id(deleted):
    calls = []
    first = "<!-- omnigent-issue-prioritization-v2 {} -->\nFirst"
    second = "<!-- omnigent-issue-prioritization-v2 {} -->\nSecond"

    def transport(method, path, payload):
        calls.append((method, path))
        if path == "/issues/comments/42":
            if deleted:
                raise GitHubNotFound("deleted")
            return {"id": 42}
        if method == "GET":
            return []
        return {"id": 43}

    client = GitHubClient("token", "org/repo", transport)
    assert client.sync_issue_comment(7, second, BotState(7, None, (), 42, first)) == (
        43 if deleted else 42,
        second,
        True,
    )
    expected = [("PATCH", "/issues/comments/42")]
    if deleted:
        expected.extend(
            [
                ("GET", "/issues/7/comments?per_page=100&page=1"),
                ("POST", "/issues/7/comments"),
            ]
        )
    assert calls == expected


def test_events_are_memoized_per_issue_across_planning_and_apply():
    calls = []

    def transport(method, path, payload):
        calls.append(path)
        return [
            {"id": 1, "event": "labeled", "label": {"name": "P2-medium"}, "actor": {"login": "bot"}}
        ]

    client = GitHubClient("token", "org/repo", transport)
    ownership = GitHubLegacyPriorityOwnership(client, {"bot"})
    planner = MutationPlanner(_manifest(), FakeStates({}), ownership)
    assert planner.resolve_state(1, ("P2-medium",), None) == BotState(1, "P2-medium", ())
    assert planner.resolve_state(1, ("P2-medium",), None) == BotState(1, "P2-medium", ())
    assert client.priority_label_actor(1, "P1-high") is None
    assert calls == ["/issues/1/events?per_page=100&page=1"]


@pytest.mark.parametrize(
    "code,headers,detail,delay",
    [
        (429, {"Retry-After": "7"}, "rate limit", 7),
        (403, {"Retry-After": "3"}, "secondary rate limit", 3),
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1100"}, "limit", 100),
        (403, {}, "You have exceeded a secondary rate limit", 60),
        (503, {}, "unavailable", 1),
    ],
)
def test_request_retries_transient_http_errors(monkeypatch, code, headers, detail, delay):
    import io

    from issue_prioritization import github

    calls = []
    sleeps = []

    def open_request(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise HTTPError(request.full_url, code, "failure", headers, io.BytesIO(detail.encode()))
        return io.BytesIO(b'{"number": 1}')

    monkeypatch.setattr(github, "urlopen", open_request)
    monkeypatch.setattr(github.time, "sleep", sleeps.append)
    monkeypatch.setattr(github.time, "time", lambda: 1000)
    assert GitHubClient("token", "org/repo").issue_data(1) == {"number": 1}
    assert len(calls) == 2
    assert sleeps == [delay]


def test_request_timeout_backoff_and_configurable_attempt_limit(monkeypatch):
    from issue_prioritization import github

    calls = []
    sleeps = []

    def open_request(request, timeout):
        calls.append(request)
        raise TimeoutError("timed out")

    monkeypatch.setattr(github, "urlopen", open_request)
    monkeypatch.setattr(github.time, "sleep", sleeps.append)
    monkeypatch.setenv("TRIAGE_GITHUB_MAX_ATTEMPTS", "3")
    with pytest.raises(RuntimeError, match="timed out"):
        GitHubClient("token", "org/repo").issue_data(1)
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_request_does_not_retry_permission_denied(monkeypatch):
    import io

    from issue_prioritization import github

    def open_request(request, timeout):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b"denied"))

    monkeypatch.setattr(github, "urlopen", open_request)
    monkeypatch.setattr(github.time, "sleep", lambda _: pytest.fail("Unexpected retry"))
    with pytest.raises(RuntimeError, match="403 denied"):
        GitHubClient("token", "org/repo").issue_data(1)


def test_request_reconciles_comment_creation_after_a_lost_response(monkeypatch):
    import io
    import json

    from issue_prioritization import github

    methods = []

    def open_request(request, timeout):
        methods.append(request.method)
        if request.method == "POST":
            raise TimeoutError("response lost after creation")
        return io.BytesIO(json.dumps([{"id": 42, "body": "triage body"}]).encode())

    monkeypatch.setattr(github, "urlopen", open_request)
    monkeypatch.setattr(github.time, "sleep", lambda _: None)
    result = GitHubClient("token", "org/repo")._request(
        "POST", "/issues/1/comments", {"body": "triage body"}
    )
    assert result["id"] == 42
    assert methods == ["POST", "GET"]
