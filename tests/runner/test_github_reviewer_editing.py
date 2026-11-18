"""GitHub PR reviewer editing: the candidate list, edits, and request validation."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.runner import create_runner_app, pr_resource
from omnigent.runner import github_resource as github
from omnigent.runner.git_providers import ReviewerRef
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests.runner.helpers import NullServerClient

PR_URL = "https://github.com/acme/widgets/pull/17"
ENTERPRISE_PR_URL = "https://ghe.example.com/acme/widgets/pull/17"
GITLAB_MR_URL = "https://gitlab.com/acme/widgets/-/merge_requests/17"
REVIEWERS_ENDPOINT = "repos/acme/widgets/pulls/17/requested_reviewers"
HOST = ["api", "--hostname", "github.com"]

GhCall = tuple[list[str], str | None]
GhResponse = tuple[int | None, str, str]


def _fake_gh(calls: list[GhCall], responses: list[GhResponse]) -> Callable[..., GhResponse]:
    """A ``_gh`` stand-in that records ``(argv, token)`` and answers in order."""

    def gh(argv: list[str], *, cwd: str, token: str | None = None) -> GhResponse:
        assert cwd == "/workspace"
        calls.append((argv, token))
        return responses.pop(0)

    return gh


def _users_page(logins: list[str], cursor: str | None = None) -> GhResponse:
    nodes = [{"login": login, "name": login.title() if login != "bot" else ""} for login in logins]
    page = {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}
    return 0, json.dumps({"data": {"repository": {"assignableUsers": page}}}), ""


def _reviewers_view(requested: list[dict[str, str]], reviews: list[tuple[str, str]]) -> GhResponse:
    data = {
        "reviewRequests": requested,
        "latestReviews": [
            {"author": {"login": login}, "state": state} for login, state in reviews
        ],
    }
    return 0, json.dumps(data), ""


@pytest.fixture
def reference(monkeypatch: pytest.MonkeyPatch) -> PullRequestRef:
    monkeypatch.setattr(github, "_pr_token", lambda _root, _reference: "selected-token")
    return PullRequestRef.from_url(PR_URL)


def test_review_candidates_page_users_then_list_teams_as_the_pr_account(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef
) -> None:
    calls: list[GhCall] = []
    teams = [{"slug": "web", "name": "Web"}, {"slug": "Core", "name": ""}, {"name": "no slug"}]
    monkeypatch.setattr(
        github,
        "_gh",
        _fake_gh(
            calls,
            [
                _users_page(["zed", "bot"], cursor="page-2"),
                _users_page(["Alice"]),
                (0, json.dumps(teams), ""),
            ],
        ),
    )

    result = github._review_candidates("/workspace", reference)

    assert result == {
        "object": "list",
        "data": [
            {"login": "Alice", "kind": "user", "name": "Alice"},
            {"login": "bot", "kind": "user", "name": None},
            {"login": "zed", "kind": "user", "name": "Zed"},
            {"login": "Core", "kind": "team", "name": None},
            {"login": "web", "kind": "team", "name": "Web"},
        ],
        "has_more": False,
    }
    assert {token for _argv, token in calls} == {"selected-token"}
    first, second, team_call = (argv for argv, _token in calls)
    assert first[:4] == ["api", "graphql", "--hostname", "github.com"]
    assert first[6:10] == ["-f", "owner=acme", "-f", "name=widgets"]
    assert "after=page-2" not in first and second[-2:] == ["-f", "after=page-2"]
    assert team_call == [*HOST, "repos/acme/widgets/teams?per_page=100"]


def test_review_candidates_stop_at_the_cap_and_report_more(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef
) -> None:
    monkeypatch.setattr(github, "_MAX_REVIEW_CANDIDATES", 2)
    calls: list[GhCall] = []
    monkeypatch.setattr(
        github,
        "_gh",
        _fake_gh(
            calls,
            [
                _users_page(["a"], cursor="2"),
                _users_page(["b", "c"], cursor="3"),
                (1, "", "gh: Not Found (HTTP 404)"),
            ],
        ),
    )

    result = github._review_candidates("/workspace", reference)

    assert [candidate["login"] for candidate in result["data"]] == ["a", "b"]
    assert result["has_more"] is True
    assert len(calls) == 3


@pytest.mark.parametrize(
    "response",
    [
        (1, "", "GraphQL: Could not resolve to a Repository"),
        (0, json.dumps({"data": {"repository": None}}), ""),
        (0, "not json", ""),
    ],
)
def test_review_candidate_failures_are_user_facing(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef, response: GhResponse
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], [response]))

    with pytest.raises(ValueError, match="could not list this repository's reviewers"):
        github._review_candidates("/workspace", reference)


def test_enterprise_reviewers_require_a_signed_in_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_list_accounts", lambda _root: (True, [{"host": "github.com"}]))
    monkeypatch.setattr(github, "_gh", _fake_gh([], []))
    reference = PullRequestRef.from_url(ENTERPRISE_PR_URL)
    octocat = ReviewerRef(login="octocat", kind="user")

    with pytest.raises(ValueError, match="Sign in to this GitHub host"):
        github._review_candidates("/workspace", reference)
    with pytest.raises(ValueError, match="Sign in to this GitHub host"):
        github._edit_reviewers("/workspace", reference, add=[octocat], remove=[])


def test_edit_reviewers_withdraws_then_requests_and_returns_review_states(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef
) -> None:
    calls: list[GhCall] = []
    final = _reviewers_view(
        [{"__typename": "User", "login": "alice"}, {"__typename": "Team", "slug": "core"}],
        [("alice", "APPROVED"), ("bob", "COMMENTED")],
    )
    monkeypatch.setattr(github, "_gh", _fake_gh(calls, [(0, "{}", ""), (0, "{}", ""), final]))

    result = github._edit_reviewers(
        "/workspace",
        reference,
        add=[ReviewerRef("alice", "user"), ReviewerRef("core", "team")],
        remove=[ReviewerRef("carol", "user"), ReviewerRef("web", "team")],
    )

    assert result == {
        "object": "session.github.pr_reviewers",
        "reviewers": [
            {"login": "alice", "kind": "user", "state": "PENDING"},
            {"login": "bob", "kind": "user", "state": "COMMENTED"},
            {"login": "core", "kind": "team", "state": "PENDING"},
        ],
    }
    assert calls == [
        (
            [
                *HOST,
                "-X",
                "DELETE",
                REVIEWERS_ENDPOINT,
                "-f",
                "reviewers[]=carol",
                "-f",
                "team_reviewers[]=web",
            ],
            "selected-token",
        ),
        (
            [
                *HOST,
                "-X",
                "POST",
                REVIEWERS_ENDPOINT,
                "-f",
                "reviewers[]=alice",
                "-f",
                "team_reviewers[]=core",
            ],
            "selected-token",
        ),
        (
            [
                "pr",
                "view",
                "17",
                "-R",
                "github.com/acme/widgets",
                "--json",
                "latestReviews,reviewRequests",
            ],
            "selected-token",
        ),
    ]


def test_withdrawing_only_teams_still_sends_the_required_reviewers_array(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef
) -> None:
    """GitHub refuses a withdrawal without ``reviewers``: '"reviewers" wasn't supplied'."""
    calls: list[GhCall] = []
    final = _reviewers_view([], [])
    monkeypatch.setattr(github, "_gh", _fake_gh(calls, [(0, "{}", ""), (0, "{}", ""), final]))

    github._edit_reviewers(
        "/workspace",
        reference,
        add=[ReviewerRef("core", "team")],
        remove=[ReviewerRef("web", "team")],
    )

    withdraw, request = (argv for argv, _token in calls[:2])
    assert withdraw[-4:] == ["-f", "reviewers[]", "-f", "team_reviewers[]=web"]
    assert request[-4:] == ["-f", "reviewers[]", "-f", "team_reviewers[]=core"]


@pytest.mark.parametrize(
    "responses, message",
    [
        (
            [(1, json.dumps({"message": "Not Found"}), "gh: Not Found (HTTP 404)")],
            "cannot edit this pull request's reviewers",
        ),
        (
            [
                (
                    1,
                    json.dumps({"message": "Reviews may only be requested from collaborators."}),
                    "gh: Validation Failed (HTTP 422)",
                )
            ],
            "GitHub rejected the reviewer change: Reviews may only be requested from collab",
        ),
        ([(1, "", "gh: Server Error (HTTP 502)")], "could not update the reviewers"),
        (
            [(0, "{}", ""), (1, "", "GraphQL: something broke")],
            "updated the reviewers but could not list them",
        ),
    ],
)
def test_edit_reviewer_failures_are_user_facing(
    monkeypatch: pytest.MonkeyPatch,
    reference: PullRequestRef,
    responses: list[GhResponse],
    message: str,
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], responses))

    with pytest.raises(ValueError, match=message):
        github._edit_reviewers(
            "/workspace", reference, add=[ReviewerRef("octocat", "user")], remove=[]
        )


def test_a_reviewer_edit_past_its_deadline_reports_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline cuts the slow request; nothing runs after it."""
    monkeypatch.setattr(github._config, "github_account_preference", lambda _repo: None)
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

    with pytest.raises(ValueError, match="took too long to update the reviewers"):
        github._edit_reviewers(
            "/workspace",
            PullRequestRef.from_url(PR_URL),
            add=[ReviewerRef("octocat", "user")],
            remove=[ReviewerRef("carol", "user")],
        )

    assert len(commands) == 1
    assert github._gh_timed_out.get() is False


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


OCTOCAT = {"login": "octocat", "kind": "user"}


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"add": [], "remove": []}, "Expected reviewers to add or remove"),
        ({"add": OCTOCAT}, "Expected add to be a list of reviewers"),
        ({"add": ["octocat"]}, "Expected add to be a list of reviewers"),
        ({"remove": [{"login": "octocat", "kind": "bot"}]}, "Expected remove to be a list"),
        ({"add": [{"login": " ", "kind": "user"}]}, "Expected add to be a list of reviewers"),
        ({"add": [OCTOCAT], "remove": [{"login": "OctoCat", "kind": "user"}]}, "both added"),
        (
            {"add": [{"login": f"user-{index}", "kind": "user"} for index in range(101)]},
            "at most 100 reviewers",
        ),
        ({"pr_url": None, "add": [OCTOCAT]}, "Select a pull request to edit its reviewers"),
        ({"pr_url": PR_URL.replace("17", "18"), "add": [OCTOCAT]}, "not associated"),
        ({"pr_url": GITLAB_MR_URL, "add": [OCTOCAT]}, "GitLab merge request reviewers"),
    ],
)
def test_invalid_reviewer_edits_never_reach_github(
    tracked: str, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object], message: str
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], []))
    request: dict[str, object] = {"pr_url": PR_URL, **kwargs}

    with pytest.raises(ValueError, match=message):
        pr_resource.update_pr_reviewers(tracked, session_id="session", **request)  # type: ignore[arg-type]


def test_reviewer_edits_deduplicate_ignoring_case_and_keep_users_and_teams_apart(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def edit(
        _root: str,
        _reference: PullRequestRef,
        *,
        add: list[ReviewerRef],
        remove: list[ReviewerRef],
    ):
        seen.update(add=add, remove=remove)
        return {"object": "session.github.pr_reviewers", "reviewers": []}

    monkeypatch.setattr(github, "_edit_reviewers", edit)

    pr_resource.update_pr_reviewers(
        tracked,
        session_id="session",
        pr_url=PR_URL,
        add=[OCTOCAT, {"login": "OctoCat", "kind": "user"}, {"login": "octocat", "kind": "team"}],
        remove=[{"login": "carol", "kind": "user"}],
    )

    assert seen == {
        "add": [ReviewerRef("octocat", "user"), ReviewerRef("octocat", "team")],
        "remove": [ReviewerRef("carol", "user")],
    }


def test_review_candidates_need_a_reviewer_editing_provider(tracked: str) -> None:
    with pytest.raises(ValueError, match="Select a pull request"):
        pr_resource.pr_review_candidates(tracked, session_id="session")
    with pytest.raises(ValueError, match="GitLab merge request reviewers"):
        pr_resource.pr_review_candidates(tracked, session_id="session", pr_url=GITLAB_MR_URL)


async def test_runner_routes_list_and_edit_the_selected_prs_reviewers(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def gh(argv: list[str], **_kwargs: object) -> GhResponse:
        calls.append(argv)
        if argv[:2] == ["api", "graphql"]:
            return _users_page(["octocat"])
        if argv[:2] == ["pr", "view"]:
            return _reviewers_view([{"__typename": "User", "login": "octocat"}], [])
        return 0, "[]" if argv[-1].endswith("teams?per_page=100") else "{}", ""

    monkeypatch.setattr(github, "_gh", gh)
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        listed = await client.get(
            "/v1/sessions/session/resources/github/reviewers", params={"pr_url": PR_URL}
        )
        edited = await client.post(
            "/v1/sessions/session/resources/github/reviewers",
            json={"pr_url": PR_URL, "add": [OCTOCAT]},
        )
        missing_url = await client.post(
            "/v1/sessions/session/resources/github/reviewers", json={"add": [OCTOCAT]}
        )
        malformed = await client.post(
            "/v1/sessions/session/resources/github/reviewers",
            json={"pr_url": PR_URL, "add": ["octocat"]},
        )

    assert listed.status_code == 200, listed.text
    assert listed.json() == {
        "object": "list",
        "data": [{"login": "octocat", "kind": "user", "name": "Octocat"}],
        "has_more": False,
    }
    assert edited.status_code == 200, edited.text
    assert edited.json() == {
        "object": "session.github.pr_reviewers",
        "reviewers": [{"login": "octocat", "kind": "user", "state": "PENDING"}],
    }
    assert (missing_url.status_code, missing_url.json()["detail"]) == (
        400,
        "Expected a pull request URL",
    )
    assert malformed.status_code == 400
    assert "list of reviewers" in malformed.json()["detail"]
    assert [argv[:2] for argv in calls] == [
        ["api", "graphql"],
        ["api", "--hostname"],
        ["api", "--hostname"],
        ["pr", "view"],
    ]
