"""GitHub PR reviewer and label shaping and panel integration coverage."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from omnigent.runner import github_resource
from omnigent.runner.session_prs import PullRequestRef


def _review(login: str | None, state: str) -> dict[str, object]:
    return {"author": {"login": login} if login is not None else None, "state": state}


def _states(rows: list[dict[str, str]]) -> dict[tuple[str, str], str]:
    return {(row["kind"], row["login"].casefold()): row["state"] for row in rows}


def test_shape_reviewers_keeps_pending_user_and_team_with_same_slug_separate() -> None:
    rows = github_resource._shape_reviewers(
        {
            "reviewRequests": [
                {"__typename": "User", "login": "platform"},
                {"__typename": "Team", "slug": "platform"},
            ]
        }
    )

    assert _states(rows) == {
        ("user", "platform"): "PENDING",
        ("team", "platform"): "PENDING",
    }


def test_shape_reviewers_reports_all_submitted_states() -> None:
    states = ("APPROVED", "COMMENTED", "CHANGES_REQUESTED", "DISMISSED")
    rows = github_resource._shape_reviewers(
        {"latestReviews": [_review(f"reviewer-{state}", state) for state in states]}
    )

    assert _states(rows) == {("user", f"reviewer-{state}".casefold()): state for state in states}


def test_shape_reviewers_deduplicates_case_insensitively_and_rerequest_is_pending() -> None:
    rows = github_resource._shape_reviewers(
        {
            "latestReviews": [_review("Alice", "APPROVED")],
            "reviewRequests": [
                {"__typename": "User", "login": "alice"},
                {"__typename": "User", "login": "ALICE"},
            ],
        }
    )

    assert rows == [{"login": "ALICE", "kind": "user", "state": "PENDING"}]


def test_shape_reviewers_skips_malformed_deleted_and_unsubmitted_reviews() -> None:
    rows = github_resource._shape_reviewers(
        {
            "latestReviews": [
                None,
                {"author": None, "state": "APPROVED"},
                {"author": {"login": None}, "state": "APPROVED"},
                {"author": {"login": "pending-user"}, "state": "PENDING"},
                {"author": {"login": ""}, "state": "COMMENTED"},
            ]
        }
    )

    assert rows == []


def test_shape_reviewers_handles_missing_and_malformed_top_level_collections() -> None:
    assert github_resource._shape_reviewers({}) == []
    assert (
        github_resource._shape_reviewers({"latestReviews": {}, "reviewRequests": "invalid"}) == []
    )


def test_shape_reviewers_skips_malformed_requests_and_uses_team_name_fallback() -> None:
    rows = github_resource._shape_reviewers(
        {
            "reviewRequests": [
                None,
                [],
                {},
                {"__typename": "User"},
                {"__typename": "User", "login": ""},
                {"__typename": "Team", "name": "Data Platform"},
                {"__typename": "User", "login": "bob"},
            ]
        }
    )

    assert rows == [
        {"login": "Data Platform", "kind": "team", "state": "PENDING"},
        {"login": "bob", "kind": "user", "state": "PENDING"},
    ]


def _pr_data() -> dict[str, object]:
    return {
        "number": 17,
        "title": "Reviewer status",
        "state": "OPEN",
        "url": "https://github.com/acme/widgets/pull/17",
        "isDraft": False,
        "author": {"login": "author"},
        "baseRefName": "main",
        "headRefName": "reviewer-status",
        "headRefOid": "head-sha",
        "baseRefOid": "base-sha",
        "statusCheckRollup": [],
        "body": "description",
        "comments": [],
        "reviews": [],
        "latestReviews": [_review("alice", "APPROVED")],
        "reviewRequests": [{"__typename": "Team", "slug": "platform"}],
        "labels": [
            {"id": "LA_1", "name": "size/XS", "color": "EDEDED", "description": "Tiny"},
            {"id": "LA_2", "name": "bug", "color": "d73a4a", "description": ""},
        ],
        "mergeStateStatus": "CLEAN",
        "autoMergeRequest": None,
    }


# The shaped form of ``_pr_data()["labels"]``.
_PR_LABELS = [
    {"name": "size/XS", "color": "ededed", "description": "Tiny"},
    {"name": "bug", "color": "d73a4a", "description": None},
]


def _assert_reviewer_fields(argv: list[str]) -> None:
    fields = argv[argv.index("--json") + 1].split(",")
    assert "latestReviews" in fields
    assert "reviewRequests" in fields
    assert "labels" in fields


def test_workspace_info_shapes_reviewers_with_one_gh_request() -> None:
    data = _pr_data()
    gh_calls: list[list[str]] = []

    def run_gh(argv: list[str], *, cwd: str, token: str | None = None) -> tuple[int, str, str]:
        del cwd, token
        gh_calls.append(argv)
        return 0, json.dumps(data), ""

    with (
        patch.object(github_resource, "_git", return_value=(0, "reviewer-status\n", "")),
        patch.object(github_resource.shutil, "which", return_value="/usr/bin/gh"),
        patch.object(github_resource, "_account_token_for", return_value=None),
        patch.object(github_resource, "_gh", side_effect=run_gh),
        patch.object(github_resource, "_list_accounts") as list_accounts,
    ):
        payload = github_resource._workspace_github_info("/workspace")

    assert payload["pr"]["reviewers"] == [
        {"login": "alice", "kind": "user", "state": "APPROVED"},
        {"login": "platform", "kind": "team", "state": "PENDING"},
    ]
    assert payload["pr"]["labels"] == _PR_LABELS
    assert len(gh_calls) == 1
    _assert_reviewer_fields(gh_calls[0])
    list_accounts.assert_not_called()


def test_reference_info_shapes_reviewers_with_one_gh_request() -> None:
    data = _pr_data()
    gh_calls: list[list[str]] = []
    reference = PullRequestRef(
        host="github.com",
        repository="acme/widgets",
        number=17,
        url="https://github.com/acme/widgets/pull/17",
    )

    def run_gh(argv: list[str], *, cwd: str, token: str | None = None) -> tuple[int, str, str]:
        del cwd, token
        gh_calls.append(argv)
        return 0, json.dumps(data), ""

    with (
        patch.object(github_resource.shutil, "which", return_value="/usr/bin/gh"),
        patch.object(
            github_resource,
            "_list_accounts",
            return_value=(True, [{"host": "github.com", "login": "alice", "active": True}]),
        ),
        patch.object(github_resource._config, "github_account_preference", return_value=None),
        patch.object(github_resource, "_pr_token", return_value=None) as pr_token,
        patch.object(github_resource, "_gh", side_effect=run_gh),
    ):
        payload = github_resource._reference_info("/workspace", reference)

    assert payload["pr"]["reviewers"] == [
        {"login": "alice", "kind": "user", "state": "APPROVED"},
        {"login": "platform", "kind": "team", "state": "PENDING"},
    ]
    assert payload["pr"]["labels"] == _PR_LABELS
    assert len(gh_calls) == 1
    _assert_reviewer_fields(gh_calls[0])
    fields = gh_calls[0][gh_calls[0].index("--json") + 1].split(",")
    assert {"headRefOid", "baseRefOid"}.issubset(fields)
    pr_token.assert_called_once_with("/workspace", reference)


@pytest.mark.parametrize("lookup", ["workspace", "reference"])
@pytest.mark.parametrize(
    "error",
    [
        'Unknown JSON field: "latestReviews"',
        'Unknown JSON field: "latestReviews"\nAvailable fields:\n'
        "  reviewRequests\n  statusCheckRollup",
        'Unknown JSON field: "reviewRequests"\nAvailable fields:\n'
        "  latestReviews\n  statusCheckRollup",
        "GraphQL: Resource not accessible by integration (repository.pullRequest.latestReviews)",
    ],
)
def test_reviewer_field_failure_preserves_pr(lookup: str, error: str) -> None:
    data = _pr_data()
    del data["reviewRequests"], data["latestReviews"]
    data["statusCheckRollup"] = [
        {
            "name": "unit",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "detailsUrl": "https://example.com/check",
        }
    ]
    reference = PullRequestRef(
        host="github.com", repository="acme/widgets", number=17, url=str(data["url"])
    )

    def run_gh(argv: list[str], *, cwd: str, token: str | None = None) -> tuple[int, str, str]:
        fields = argv[-1].split(",")
        if "latestReviews" in fields or "reviewRequests" in fields:
            return 1, "", error
        return 0, json.dumps({field: data[field] for field in fields}), ""

    with (
        patch.object(github_resource, "_git", return_value=(0, "reviewer-status\n", "")),
        patch.object(github_resource.shutil, "which", return_value="/usr/bin/gh"),
        patch.object(github_resource, "_account_token_for", return_value="selected-token"),
        patch.object(github_resource, "_pr_token", return_value="selected-token"),
        patch.object(github_resource, "_list_accounts", return_value=(True, [])),
        patch.object(github_resource._config, "github_account_preference", return_value=None),
        patch.object(github_resource, "_gh", side_effect=run_gh) as gh_mock,
    ):
        payload = (
            github_resource._workspace_github_info("/workspace")
            if lookup == "workspace"
            else github_resource._reference_info("/workspace", reference)
        )

    assert payload["authenticated"] is True
    assert payload["pr"]["title"] == data["title"]
    assert payload["pr"]["body"] == data["body"]
    assert payload["pr"]["checks_supported"] is True
    assert payload["pr"]["checks"] == {
        "passing": 1,
        "failing": 0,
        "pending": 0,
        "total": 1,
        "runs": [{"name": "unit", "bucket": "passing", "url": "https://example.com/check"}],
    }
    assert "reviewers" not in payload["pr"]
    first_call, fallback_call = gh_mock.call_args_list
    _assert_reviewer_fields(first_call.args[0])
    assert fallback_call.args[0][:-1] == first_call.args[0][:-1]
    assert (
        fallback_call.kwargs
        == first_call.kwargs
        == {
            "cwd": "/workspace",
            "token": "selected-token",
        }
    )
    assert fallback_call.args[0][-1].split(",") == [
        field
        for field in first_call.args[0][-1].split(",")
        if field not in {"reviewRequests", "latestReviews"}
    ]


def test_checks_field_failure_preserves_reviewers() -> None:
    data = _pr_data()
    error = (
        'Unknown JSON field: "statusCheckRollup"\nAvailable fields:\n'
        "  latestReviews\n  reviewRequests"
    )

    def run_gh(argv: list[str], *, cwd: str, token: str | None = None) -> tuple[int, str, str]:
        fields = argv[-1].split(",")
        if "statusCheckRollup" in fields:
            return 1, "", error
        return 0, json.dumps({field: data[field] for field in fields}), ""

    with patch.object(github_resource, "_gh", side_effect=run_gh) as gh_mock:
        result = github_resource._gh_pr_view([], github_resource._PR_VIEW_FIELDS, cwd="/workspace")

    assert result == {
        field: value
        for field, value in data.items()
        if field in github_resource._PR_VIEW_FIELDS.split(",") and field != "statusCheckRollup"
    }
    assert gh_mock.call_count == 2
    _assert_reviewer_fields(gh_mock.call_args.args[0])


def test_title_only_failure_does_not_retry() -> None:
    reference = PullRequestRef(
        host="github.com",
        repository="acme/widgets",
        number=17,
        url="https://github.com/acme/widgets/pull/17",
    )
    with (
        patch.object(github_resource, "_pr_token", return_value=None),
        patch.object(github_resource, "_gh", return_value=(1, "", "unavailable")) as run_gh,
    ):
        assert github_resource._pr_json("/workspace", reference, "title") is None
    run_gh.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        "network offline",
        "no pull requests found",
        "authentication failed",
        'Unknown JSON field: "notSupported"\nAvailable fields:\n'
        "  latestReviews\n  reviewRequests\n  statusCheckRollup",
        "GraphQL: Resource not accessible (repository.pullRequest.latestReviewsCount)",
    ],
)
def test_unrelated_pr_failure_does_not_retry(error: str) -> None:
    reference = PullRequestRef(
        host="github.com",
        repository="acme/widgets",
        number=17,
        url="https://github.com/acme/widgets/pull/17",
    )
    with (
        patch.object(github_resource, "_pr_token", return_value=None),
        patch.object(github_resource, "_gh", return_value=(1, "", error)) as run_gh,
    ):
        assert (
            github_resource._pr_json("/workspace", reference, github_resource._PR_VIEW_FIELDS)
            is None
        )
    run_gh.assert_called_once()


def test_commit_resolved_pr_preserves_data_without_reviewer_fields() -> None:
    data = _pr_data()
    del data["reviewRequests"], data["latestReviews"]
    with (
        patch.object(github_resource, "_resolve_pr_via_commit", return_value=(17, "acme/widgets")),
        patch.object(github_resource, "_resolved_base_nwo", return_value="acme/widgets"),
        patch.object(
            github_resource,
            "_gh",
            side_effect=[
                (1, "", "no pull requests found for branch"),
                (1, "", 'Unknown JSON field: "latestReviews"'),
                (0, json.dumps(data), ""),
            ],
        ) as run_gh,
    ):
        assert github_resource._pr_view_json("/workspace", github_resource._PR_VIEW_FIELDS) == data
    assert run_gh.call_args.args[0][:5] == ["pr", "view", "17", "-R", "acme/widgets"]


@pytest.mark.parametrize(
    "errors",
    [
        ['Unknown JSON field: "latestReviews"', "Resource not accessible (statusCheckRollup)"],
        ["Resource not accessible (statusCheckRollup)", 'Unknown JSON field: "latestReviews"'],
        ["Resource not accessible (statusCheckRollup, reviewRequests)"],
    ],
)
def test_checks_and_reviewer_failures_preserve_pr(errors: list[str]) -> None:
    data = _pr_data()
    del data["statusCheckRollup"], data["reviewRequests"], data["latestReviews"]
    reference = PullRequestRef.from_url(str(data["url"]))
    with (
        patch.object(github_resource.shutil, "which", return_value="/usr/bin/gh"),
        patch.object(github_resource, "_list_accounts", return_value=(True, [])),
        patch.object(github_resource._config, "github_account_preference", return_value=None),
        patch.object(github_resource, "_pr_token", return_value="selected-token"),
        patch.object(
            github_resource,
            "_gh",
            side_effect=[*((1, "", error) for error in errors), (0, json.dumps(data), "")],
        ) as run_gh,
    ):
        payload = github_resource._reference_info("/workspace", reference)

    assert payload["authenticated"] is True
    assert payload["pr"]["title"] == data["title"]
    assert payload["pr"]["checks_supported"] is False
    assert "reviewers" not in payload["pr"]
    assert run_gh.call_count == len(errors) + 1
    for call in run_gh.call_args_list:
        assert call.kwargs == {"cwd": "/workspace", "token": "selected-token"}
    assert {"headRefOid", "baseRefOid", "body", "comments"}.issubset(
        run_gh.call_args.args[0][-1].split(",")
    )
