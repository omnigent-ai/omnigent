"""GitHub PR merging: merge-state shaping, merge options, the merge, and request validation."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.runner import create_runner_app, pr_resource
from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests.runner.helpers import NullServerClient

PR_URL = "https://github.com/acme/widgets/pull/17"
ENTERPRISE_PR_URL = "https://ghe.example.com/acme/widgets/pull/17"
GITLAB_MR_URL = "https://gitlab.com/acme/widgets/-/merge_requests/17"
HEAD_SHA = "66331978a046d81e2f1a6772e6354f9ae43b734b"
MERGE_OPTION_FIELDS = (
    "viewerDefaultMergeMethod,mergeCommitAllowed,squashMergeAllowed,rebaseMergeAllowed"
)

GhCall = tuple[list[str], str | None]


def _fake_gh(
    calls: list[GhCall], responses: list[tuple[int | None, str, str]]
) -> Callable[..., tuple[int | None, str, str]]:
    """A ``_gh`` stand-in that records ``(argv, token)`` and answers in order."""

    def gh(argv: list[str], *, cwd: str, token: str | None = None) -> tuple[int | None, str, str]:
        assert cwd == "/workspace"
        calls.append((argv, token))
        return responses.pop(0)

    return gh


@pytest.fixture
def reference(monkeypatch: pytest.MonkeyPatch) -> PullRequestRef:
    monkeypatch.setattr(github, "_pr_token", lambda _root, _reference: "selected-token")
    return PullRequestRef.from_url(PR_URL)


def test_merge_fields_shape_state_and_auto_merge() -> None:
    assert github._merge_fields(
        {
            "mergeStateStatus": "blocked",
            "autoMergeRequest": {
                "authorEmail": "dev@example.com",
                "mergeMethod": "SQUASH",
                "enabledAt": "2026-10-21T06:02:55Z",
                "enabledBy": {"login": "dev", "name": "Dev"},
            },
        }
    ) == {
        "merge_state_status": "BLOCKED",
        "auto_merge": {
            "merge_method": "SQUASH",
            "enabled_by": "dev",
            "enabled_at": "2026-10-21T06:02:55Z",
        },
    }
    assert github._merge_fields({"mergeStateStatus": None, "autoMergeRequest": None}) == {
        "merge_state_status": None,
        "auto_merge": None,
    }
    assert github._merge_fields({"autoMergeRequest": {"enabledBy": None}}) == {
        "auto_merge": {"merge_method": None, "enabled_by": None, "enabled_at": None}
    }
    # A field an older gh dropped stays out of the payload.
    assert github._merge_fields({}) == {}


@pytest.mark.parametrize("path", ["workspace", "reference"])
def test_both_read_paths_report_merge_state(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    data = {
        "number": 17,
        "url": PR_URL,
        "state": "OPEN",
        "mergeStateStatus": "CLEAN",
        "autoMergeRequest": None,
    }
    monkeypatch.setattr(github, "_gh_pr_view", lambda *_args, **_kwargs: data)
    monkeypatch.setattr(github, "_list_accounts", lambda _root: (True, []))
    monkeypatch.setattr(github, "_account_token_for", lambda _root: None)
    monkeypatch.setattr(github, "_git", lambda *_args, **_kwargs: (0, "branch\n", ""))
    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(github, "_pr_token", lambda _root, _reference: None)

    if path == "workspace":
        info = github._workspace_github_info("/workspace")
    else:
        info = github._reference_info("/workspace", PullRequestRef.from_url(PR_URL))

    assert info["pr"]["merge_state_status"] == "CLEAN"
    assert info["pr"]["auto_merge"] is None


def test_an_old_gh_without_merge_fields_keeps_the_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def gh(argv: list[str], **_kwargs: object) -> tuple[int, str, str]:
        calls.append(argv)
        fields = argv[-1].split(",")
        if "mergeStateStatus" in fields:
            return 1, "", 'Unknown JSON field: "mergeStateStatus"\nAvailable fields:\n  title'
        return 0, json.dumps(dict.fromkeys(fields) | {"title": "t"}), ""

    monkeypatch.setattr(github, "_gh", gh)

    data = github._gh_pr_view([], github._PR_VIEW_FIELDS, cwd="/workspace")

    assert data is not None and data["title"] == "t"
    assert "mergeStateStatus" not in data
    # Only the refused field is dropped; auto-merge is still read.
    assert "autoMergeRequest" in calls[-1][-1].split(",")
    assert github._merge_fields(data) == {"auto_merge": None}


@pytest.mark.parametrize(
    "settings, expected",
    [
        (
            {
                "viewerDefaultMergeMethod": "SQUASH",
                "mergeCommitAllowed": True,
                "squashMergeAllowed": True,
                "rebaseMergeAllowed": True,
            },
            {"methods": ["merge", "squash", "rebase"], "default": "squash"},
        ),
        (
            {
                "viewerDefaultMergeMethod": "MERGE",
                "mergeCommitAllowed": False,
                "squashMergeAllowed": True,
                "rebaseMergeAllowed": True,
            },
            {"methods": ["squash", "rebase"], "default": "squash"},
        ),
        (
            {"viewerDefaultMergeMethod": "MERGE", "mergeCommitAllowed": False},
            {"methods": [], "default": None},
        ),
    ],
)
def test_merge_options_offer_allowed_methods_with_a_default(
    monkeypatch: pytest.MonkeyPatch,
    reference: PullRequestRef,
    settings: dict[str, object],
    expected: dict[str, object],
) -> None:
    calls: list[GhCall] = []
    monkeypatch.setattr(github, "_gh", _fake_gh(calls, [(0, json.dumps(settings), "")]))

    result = github._merge_options("/workspace", reference)

    assert result == {"object": "session.github.merge_options", **expected}
    assert calls == [
        (
            ["repo", "view", "github.com/acme/widgets", "--json", MERGE_OPTION_FIELDS],
            "selected-token",
        )
    ]


@pytest.mark.parametrize(
    "response, message",
    [
        ((1, "", "HTTP 404"), "could not read this repository's merge settings"),
        ((0, "not json", ""), "unexpected merge settings"),
        ((0, "null", ""), "unexpected merge settings"),
        ((0, "[]", ""), "unexpected merge settings"),
    ],
)
def test_merge_option_failures_are_user_facing(
    monkeypatch: pytest.MonkeyPatch,
    reference: PullRequestRef,
    response: tuple[int | None, str, str],
    message: str,
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], [response]))

    with pytest.raises(ValueError, match=message):
        github._merge_options("/workspace", reference)


def test_merge_runs_gh_pr_merge_pinned_to_the_head_as_the_pr_account(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef
) -> None:
    calls: list[GhCall] = []
    monkeypatch.setattr(github, "_gh", _fake_gh(calls, [(0, "", "")]))

    result = github._merge_pr(
        "/workspace", reference, merge_method="squash", expected_head=HEAD_SHA
    )

    assert result == {"object": "session.github.pr_merge", "pr_url": PR_URL}
    assert calls == [
        (
            [
                "pr",
                "merge",
                "17",
                "-R",
                "github.com/acme/widgets",
                "--squash",
                "--match-head-commit",
                HEAD_SHA,
            ],
            "selected-token",
        )
    ]


@pytest.mark.parametrize(
    "stderr, message",
    [
        (
            "GraphQL: Head branch was modified. Review and try the merge again. "
            "(mergePullRequest)",
            "branch changed since the panel loaded",
        ),
        ("HTTP 403: Resource not accessible by integration", "cannot merge this pull request"),
        (
            "X Pull request acme/widgets#17 is not mergeable: the base branch policy prohibits "
            "the merge.\nTo have the pull request merged after all the requirements...",
            "could not merge this pull request: Pull request acme/widgets#17 is not mergeable",
        ),
        ("", "could not merge this pull request. Refresh to retry"),
    ],
)
def test_merge_failures_are_user_facing(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef, stderr: str, message: str
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], [(1, "", stderr)]))

    with pytest.raises(ValueError, match=message):
        github._merge_pr("/workspace", reference, merge_method="merge", expected_head=HEAD_SHA)


def test_enterprise_merges_require_a_signed_in_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_list_accounts", lambda _root: (True, [{"host": "github.com"}]))
    monkeypatch.setattr(github, "_gh", _fake_gh([], []))
    reference = PullRequestRef.from_url(ENTERPRISE_PR_URL)

    with pytest.raises(ValueError, match="Sign in to this GitHub host"):
        github._merge_options("/workspace", reference)
    with pytest.raises(ValueError, match="Sign in to this GitHub host"):
        github._merge_pr("/workspace", reference, merge_method="merge", expected_head=HEAD_SHA)


@pytest.mark.parametrize("slow", ["account lookup", "merge"])
def test_a_merge_past_its_deadline_reports_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch, slow: str
) -> None:
    """The deadline cuts the slow call; a merge never runs as another account."""
    monkeypatch.setattr(
        github._config,
        "github_account_preference",
        lambda _repo: "work" if slow == "account lookup" else None,
    )
    clock = [100.0]
    monkeypatch.setattr(github, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    commands: list[list[str]] = []

    def run(argv: list[str], *, cwd: str, timeout: float, env: object = None):
        del cwd, env
        commands.append(argv)
        assert timeout <= github._PR_EDIT_REQUEST_SECONDS
        clock[0] += timeout
        return None, "", "timed out"

    monkeypatch.setattr(github, "_run", run)

    with pytest.raises(ValueError, match="took too long to merge"):
        github._merge_pr(
            "/workspace",
            PullRequestRef.from_url(PR_URL),
            merge_method="merge",
            expected_head=HEAD_SHA,
        )

    first = ["gh", "auth", "token"] if slow == "account lookup" else ["gh", "pr", "merge"]
    assert [command[:3] for command in commands] == [first]


@pytest.mark.parametrize("operation", ["merge", "disable auto-merge"])
def test_a_selected_account_without_a_token_never_runs_as_another(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """A saved account that gh can't sign in as stops the merge, not falls back."""
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _repo: "work")
    calls: list[GhCall] = []
    monkeypatch.setattr(
        github,
        "_gh",
        _fake_gh(calls, [(1, "", "no oauth token found for github.com account work")]),
    )
    reference = PullRequestRef.from_url(PR_URL)

    with pytest.raises(ValueError, match="account work isn't signed in on the host"):
        if operation == "merge":
            github._merge_pr("/workspace", reference, merge_method="merge", expected_head=HEAD_SHA)
        else:
            github._disable_auto_merge("/workspace", reference)

    assert [argv[:2] for argv, _token in calls] == [["auth", "token"]]


def test_without_a_selected_account_gh_runs_as_its_active_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _repo: None)
    calls: list[GhCall] = []
    monkeypatch.setattr(github, "_gh", _fake_gh(calls, [(0, "", "")]))

    github._disable_auto_merge("/workspace", PullRequestRef.from_url(PR_URL))

    assert calls == [
        (["pr", "merge", "17", "-R", "github.com/acme/widgets", "--disable-auto"], None)
    ]


@pytest.fixture
def tracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(github, "_pr_token", lambda _root, _reference: None)
    SessionPrRegistry("session").record(
        [PullRequestRef.from_url(PR_URL), PullRequestRef.from_url(GITLAB_MR_URL)],
        relationship="created",
        source="test",
    )
    return str(tmp_path)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"merge_method": None}, "Expected merge_method"),
        ({"merge_method": "fast-forward"}, "Expected merge_method"),
        ({"merge_method": "--admin"}, "Expected merge_method"),
        ({"expected_head": None}, "Expected expected_head"),
        ({"expected_head": HEAD_SHA[:7]}, "Expected expected_head"),
        ({"expected_head": f"--{HEAD_SHA[2:]}"}, "Expected expected_head"),
        ({"pr_url": None}, "Select a pull request to merge"),
        ({"pr_url": PR_URL.replace("17", "18")}, "not associated"),
        ({"pr_url": GITLAB_MR_URL}, "Merging a GitLab merge request is not supported"),
    ],
)
def test_invalid_merges_never_reach_github(
    tracked: str, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object], message: str
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], []))
    request: dict[str, object] = {
        "pr_url": PR_URL,
        "merge_method": "merge",
        "expected_head": HEAD_SHA,
        **kwargs,
    }

    with pytest.raises(ValueError, match=message):
        pr_resource.merge_pr(tracked, session_id="session", **request)  # type: ignore[arg-type]


@pytest.mark.parametrize("operation", ["pr_merge_options", "disable_pr_auto_merge"])
def test_merge_operations_need_a_merging_provider(tracked: str, operation: str) -> None:
    function = getattr(pr_resource, operation)
    with pytest.raises(ValueError, match="Select a pull request to merge"):
        function(tracked, session_id="session")
    with pytest.raises(ValueError, match="Merging a GitLab merge request"):
        function(tracked, session_id="session", pr_url=GITLAB_MR_URL)


def test_disable_auto_merge_runs_gh_as_the_pr_account(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef
) -> None:
    calls: list[GhCall] = []
    monkeypatch.setattr(github, "_gh", _fake_gh(calls, [(0, "", "")]))

    result = github._disable_auto_merge("/workspace", reference)

    assert result == {"object": "session.github.pr_merge", "pr_url": PR_URL}
    assert calls == [
        (
            ["pr", "merge", "17", "-R", "github.com/acme/widgets", "--disable-auto"],
            "selected-token",
        )
    ]


@pytest.mark.parametrize(
    "response, message",
    [
        ((1, "", "HTTP 403: Resource not accessible"), "cannot disable auto-merge"),
        ((1, "", "GraphQL: Auto merge is not allowed"), "could not disable auto-merge: Auto"),
        ((None, "", "timed out"), "could not disable auto-merge"),
    ],
)
def test_disable_auto_merge_failures_are_user_facing(
    monkeypatch: pytest.MonkeyPatch,
    reference: PullRequestRef,
    response: tuple[int | None, str, str],
    message: str,
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], [response]))

    with pytest.raises(ValueError, match=message):
        github._disable_auto_merge("/workspace", reference)


async def test_runner_routes_read_options_and_merge_the_selected_pr(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def gh(argv: list[str], **_kwargs: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:2] == ["repo", "view"]:
            return (
                0,
                json.dumps({"viewerDefaultMergeMethod": "MERGE", "mergeCommitAllowed": True}),
                "",
            )
        return 0, "", ""

    monkeypatch.setattr(github, "_gh", gh)
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        options = await client.get(
            "/v1/sessions/session/resources/github/merge-options", params={"pr_url": PR_URL}
        )
        merged = await client.post(
            "/v1/sessions/session/resources/github/merge",
            json={"pr_url": PR_URL, "merge_method": "merge", "expected_head": HEAD_SHA},
        )
        missing_url = await client.post(
            "/v1/sessions/session/resources/github/merge", json={"merge_method": "merge"}
        )
        bad_method = await client.post(
            "/v1/sessions/session/resources/github/merge",
            json={"pr_url": PR_URL, "merge_method": "octopus", "expected_head": HEAD_SHA},
        )
        disabled = await client.post(
            "/v1/sessions/session/resources/github/disable-auto-merge", json={"pr_url": PR_URL}
        )
        disable_missing_url = await client.post(
            "/v1/sessions/session/resources/github/disable-auto-merge", json={}
        )

    assert options.status_code == 200, options.text
    assert options.json() == {
        "object": "session.github.merge_options",
        "methods": ["merge"],
        "default": "merge",
    }
    assert merged.status_code == 200, merged.text
    assert merged.json() == {"object": "session.github.pr_merge", "pr_url": PR_URL}
    assert (missing_url.status_code, missing_url.json()["detail"]) == (
        400,
        "Expected a pull request URL",
    )
    assert bad_method.status_code == 400
    assert "Expected merge_method" in bad_method.json()["detail"]
    assert disabled.status_code == 200, disabled.text
    assert disabled.json() == {"object": "session.github.pr_merge", "pr_url": PR_URL}
    assert disable_missing_url.status_code == 400
    assert [argv[:2] for argv in calls] == [["repo", "view"], ["pr", "merge"], ["pr", "merge"]]
    assert calls[-1][-1] == "--disable-auto"


@pytest.mark.parametrize("provider", ["gitlab", "azure_devops"])
@pytest.mark.parametrize("operation", ["merge_options", "merge", "disable_auto_merge"])
def test_other_providers_reject_merge_operations(provider: str, operation: str) -> None:
    from importlib import import_module

    facet = import_module(f"omnigent.runner.git_providers.{provider}").PULL_REQUESTS
    kwargs = {"merge_method": "merge", "expected_head": HEAD_SHA} if operation == "merge" else {}

    assert facet.capabilities.merging is False
    with pytest.raises(ValueError, match="not supported yet"):
        getattr(facet, operation)("/workspace", PullRequestRef.from_url(PR_URL), **kwargs)
