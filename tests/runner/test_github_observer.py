"""GitHub's observer rules, read through the facet methods that the PR observer calls."""

from __future__ import annotations

import functools

import pytest

from omnigent.runner.git_providers import ShellPrOp, ShellSegment
from omnigent.runner.git_providers.github import PULL_REQUESTS
from omnigent.runner.session_prs import PullRequestRef

A = "https://github.com/example/one/pull/42"


def _segment(*tokens: str, env: tuple[str, ...] = ()) -> ShellSegment:
    return ShellSegment(raw_tokens=(*env, *tokens), invocation_tokens=tokens)


def test_pr_and_api_ops_stay_in_order_before_deferred_lookups() -> None:
    target = PullRequestRef.from_url(A)
    segments = [
        _segment("gh", "auth", "status"),
        _segment("gh", "pr", "view", "42", "-R", "example/one"),
        _segment("git", "push"),
        _segment("/usr/bin/gh", "pr", "create"),
        _segment("gh", "api", "repos/example/one/pulls/42", "-X", "PATCH"),
        _segment("gh", "pr", "diff", "42", "-R", "example/one"),
    ]

    ops = PULL_REQUESTS.shell_pr_operations(segments)

    assert ops[:-2] == [
        ShellPrOp(tracks=False, creates=False, target=target, content_only=False),
        ShellPrOp(tracks=True, creates=True, target=None, content_only=False),
        ShellPrOp(tracks=True, creates=False, target=target, content_only=False),
        ShellPrOp(tracks=False, creates=False, target=target, content_only=True),
    ]
    assert ops[-2].creates and ops[-2].resolve_result is not None
    assert not ops[-1].creates and ops[-1].resolve_result is not None


@pytest.mark.parametrize(
    "segments,mixed",
    [
        ([_segment("gh", "api", "repos/example/one", "--jq", ".permissions.push")], True),
        ([_segment("gh", "pr", "diff", "42")], True),
        ([_segment("gh", "pr", "edit", "42", "--body", "test")], False),
        ([], False),
    ],
)
def test_create_defers_a_created_lookup(segments: list[ShellSegment], mixed: bool) -> None:
    ops = PULL_REQUESTS.shell_pr_operations([*segments, _segment("gh", "pr", "create")])

    [lookup] = [op for op in ops if op.creates and op.resolve_result is not None]
    assert isinstance(lookup.resolve_result, functools.partial)
    assert lookup.resolve_result.keywords["mixed"] == mixed


def test_reads_without_a_create_defer_no_created_lookup() -> None:
    ops = PULL_REQUESTS.shell_pr_operations([_segment("gh", "pr", "diff", "42")])

    assert all(op.resolve_result is None for op in ops)


@pytest.mark.parametrize(
    "segment,url",
    [
        (_segment("gh", "-R", "example/one", "pr", "edit", "42"), A),
        (_segment("gh", "--repo=example/one", "pr", "merge", "42"), A),
        (
            _segment("gh", "pr", "edit", "42", "-R", "example/one", env=("GH_HOST=ghe.example",)),
            "https://ghe.example/example/one/pull/42",
        ),
    ],
)
def test_global_repo_and_host_options_name_the_target(segment: ShellSegment, url: str) -> None:
    [op] = PULL_REQUESTS.shell_pr_operations([segment])

    assert op.tracks
    assert op.target is not None and op.target.url == url


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("mcp__azure-devops__repo_create_pull_request", {}),
        ("mcp__github__list_pull_requests", {}),
        ("mcp__github__github_write_api_call", {"endpoint": "issues.create"}),
    ],
)
def test_other_mcp_tools_are_left_to_other_providers(
    tool_name: str, arguments: dict[str, object]
) -> None:
    assert PULL_REQUESTS.mcp_prs(tool_name, arguments, {"html_url": A}) is None


def test_a_comment_only_review_is_claimed_without_prs() -> None:
    arguments: dict[str, object] = {
        "owner": "example",
        "repo": "one",
        "pullNumber": 42,
        "event": "COMMENT",
    }

    answer = PULL_REQUESTS.mcp_prs(
        "mcp__github__create_pull_request_review", arguments, {"html_url": A}
    )

    assert answer == ([], False)
