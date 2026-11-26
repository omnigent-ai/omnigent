"""The pull requests in a stack: the walk, its caps, cycles, and lists it can't trust."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from omnigent.runner import github_resource as github
from omnigent.runner import github_stack
from omnigent.runner.session_prs import PullRequestRef

PR_URL = "https://github.com/example-org/example-repo/pull/{number}"
TOKEN = "ghp_example_token_value"


def _pr(number: int, head: str, base: str, *, cross_repository: bool = False) -> dict[str, Any]:
    return {
        "number": number,
        "url": PR_URL.format(number=number),
        "title": f"PR {number}",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": head,
        "headRefOid": f"{number:040x}",
        "baseRefName": base,
        "isCrossRepository": cross_repository,
    }


class StackGraph:
    """Answers ``gh pr list --head/--base`` and the current pull request's view from a graph."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, prs: list[dict[str, Any]], current: int
    ) -> None:
        self.prs = prs
        self.lists: list[tuple[str, str]] = []
        self.tokens: list[str | None] = []
        self.token_lookups = 0
        # Branches whose list fails, or runs past the walk's deadline.
        self.failing: set[str] = set()
        self.timing_out: set[str] = set()
        self.default_branch: str | None = "main"
        self.deadlines: list[float | None] = []
        self.current = next(pr for pr in prs if pr["number"] == current)
        monkeypatch.setattr(github, "_pr_token", self._token)
        monkeypatch.setattr(github, "_gh", self._gh)
        monkeypatch.setattr(github, "_pr_json", lambda _root, _reference, _fields: self.current)

    def _token(self, _root: str, _reference: PullRequestRef) -> str:
        self.token_lookups += 1
        return TOKEN

    def _gh(
        self, argv: list[str], *, cwd: str, token: str | None = None, timeout: float | None = None
    ) -> tuple[int, str, str]:
        # Every call runs under the walk's one deadline.
        deadline = github._gh_deadline.get()
        assert deadline is not None
        assert deadline <= time.monotonic() + github_stack.WALK_SECONDS
        self.deadlines.append(deadline)
        if argv[:2] == ["repo", "view"]:
            if self.default_branch is None:
                return 1, "", "HTTP 502"
            return 0, json.dumps({"defaultBranchRef": {"name": self.default_branch}}), ""
        flag = "--head" if "--head" in argv else "--base"
        branch = argv[argv.index(flag) + 1]
        self.lists.append((flag, branch))
        self.tokens.append(token)
        if branch in self.timing_out:
            github._gh_timed_out.set(True)
            return 1, "", "deadline"
        if branch in self.failing:
            return 1, "", "HTTP 502"
        field = "headRefName" if flag == "--head" else "baseRefName"
        return 0, json.dumps([pr for pr in self.prs if pr[field] == branch]), ""


def _reference(number: int = 1) -> PullRequestRef:
    return PullRequestRef.from_url(PR_URL.format(number=number))


def _chain(length: int) -> list[dict[str, Any]]:
    """A linear stack: pull request 1 on main, each next one on the one before."""
    prs = [_pr(1, "branch-1", "main")]
    for number in range(2, length + 1):
        prs.append(_pr(number, f"branch-{number}", f"branch-{number - 1}"))
    return prs


def _numbers(stack: dict[str, Any]) -> list[int]:
    return sorted(member["number"] for member in stack["members"])


def test_a_linear_stack_lists_every_member_once_and_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    StackGraph(monkeypatch, _chain(3), current=2)

    stack = github_stack.pull_request_stack("/workspace", _reference(2))

    assert _numbers(stack) == [1, 2, 3]
    assert [m["number"] for m in stack["members"] if m["current"]] == [2]
    assert stack["truncated"] is False
    parent = next(member for member in stack["members"] if member["number"] == 1)
    assert parent == {
        "pr_url": PR_URL.format(number=1),
        "number": 1,
        "title": "PR 1",
        "state": "OPEN",
        "is_draft": False,
        "head_ref": "branch-1",
        "head_sha": f"{1:040x}",
        "base_ref": "main",
        "current": False,
    }


def test_every_list_runs_as_the_account_looked_up_once(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = StackGraph(monkeypatch, _chain(4), current=2)

    github_stack.pull_request_stack("/workspace", _reference(2))

    assert graph.token_lookups == 1
    assert graph.tokens and set(graph.tokens) == {TOKEN}


def test_a_cycle_marks_the_stack_truncated_instead_of_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pull request 2's base is branch-1, and pull request 1's base is branch-2: a loop.
    prs = [_pr(1, "branch-1", "branch-2"), _pr(2, "branch-2", "branch-1")]
    StackGraph(monkeypatch, prs, current=1)

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert stack["truncated"] is True
    assert _numbers(stack) == [1, 2]


def test_a_stack_deeper_than_the_cap_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    StackGraph(monkeypatch, _chain(github_stack.MAX_DEPTH + 3), current=1)

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert stack["truncated"] is True
    assert max(_numbers(stack)) == github_stack.MAX_DEPTH + 1


def test_the_walk_stops_at_the_gh_call_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_stack, "MAX_DEPTH", 50)
    graph = StackGraph(monkeypatch, _chain(github_stack.MAX_GH_CALLS + 5), current=1)

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert stack["truncated"] is True
    # The default branch's read is one of the calls.
    assert len(graph.lists) == github_stack.MAX_GH_CALLS - 1


def test_a_stack_within_both_caps_is_not_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = StackGraph(monkeypatch, _chain(4), current=1)

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert stack["truncated"] is False
    assert _numbers(stack) == [1, 2, 3, 4]
    assert len(graph.lists) <= github_stack.MAX_GH_CALLS


def test_a_failed_list_marks_the_stack_truncated_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = StackGraph(monkeypatch, _chain(4), current=2)
    graph.failing.add("branch-3")  # The list of what's stacked on pull request 3.

    stack = github_stack.pull_request_stack("/workspace", _reference(2))

    assert stack["truncated"] is True
    assert _numbers(stack) == [1, 2, 3]


def test_a_list_cut_at_its_limit_marks_the_stack_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_stack, "MAX_GH_CALLS", 100)
    children = [_pr(number, f"child-{number}", "branch-1") for number in range(2, 32)]
    StackGraph(monkeypatch, [_pr(1, "branch-1", "main"), *children], current=1)

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert stack["truncated"] is True


def test_the_walk_stops_when_its_deadline_cuts_a_list_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = StackGraph(monkeypatch, _chain(4), current=1)
    graph.timing_out.add("branch-2")

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert stack["truncated"] is True
    assert _numbers(stack) == [1, 2]
    assert ("--base", "branch-3") not in graph.lists
    assert len(set(graph.deadlines)) == 1


def test_another_repositorys_branch_with_the_same_name_is_not_in_the_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A contributor's pull request from their own repository's ``main`` branch.
    prs = [_pr(1, "branch-1", "main"), _pr(2, "main", "release", cross_repository=True)]
    StackGraph(monkeypatch, prs, current=1)

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert _numbers(stack) == [1]
    assert stack["truncated"] is False


def test_a_pull_request_from_another_repository_has_nothing_stacked_on_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pull request 2 targets this repository's own branch-1, not the contributor's.
    prs = [_pr(1, "branch-1", "main", cross_repository=True), _pr(2, "branch-2", "branch-1")]
    graph = StackGraph(monkeypatch, prs, current=1)

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert _numbers(stack) == [1]
    assert ("--base", "branch-1") not in graph.lists


def test_a_pull_request_github_cant_load_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = StackGraph(monkeypatch, _chain(2), current=1)
    monkeypatch.setattr(github, "_pr_json", lambda _root, _reference, _fields: None)

    with pytest.raises(ValueError) as refused:
        github_stack.pull_request_stack("/workspace", _reference(1))

    assert str(refused.value) == "GitHub could not load this pull request. Refresh to retry."
    assert graph.lists == []


def test_the_walk_never_looks_past_the_default_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    # A backport from main, and a PR that heads main into a release branch.
    prs = [*_chain(2), _pr(9, "main", "release-1")]
    graph = StackGraph(monkeypatch, prs, current=1)

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert _numbers(stack) == [1, 2]
    assert ("--head", "main") not in graph.lists
    assert stack["truncated"] is False


def test_a_long_lived_branch_with_its_release_prs_is_a_base_not_a_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A develop branch, released to main twice, with a feature PR and one stacked on it.
    prs = [
        _pr(1, "develop", "main"),
        _pr(2, "develop", "main"),
        _pr(3, "feature", "develop"),
        _pr(4, "feature-part-2", "feature"),
    ]
    StackGraph(monkeypatch, prs, current=4)

    stack = github_stack.pull_request_stack("/workspace", _reference(4))

    assert _numbers(stack) == [3, 4]
    assert stack["truncated"] is False


def test_without_the_default_branch_the_walk_still_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = StackGraph(monkeypatch, _chain(3), current=2)
    graph.default_branch = None

    stack = github_stack.pull_request_stack("/workspace", _reference(2))

    assert _numbers(stack) == [1, 2, 3]
