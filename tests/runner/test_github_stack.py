"""The pull requests in a stack: the walk, its caps, cycles, and lists it can't trust."""

from __future__ import annotations

import json
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
    """Answers every ``gh`` process a stack read starts, from a graph of pull requests.

    Only the process is faked, so ``gh``'s own deadline and token handling, the account
    lookup and the pull request's read run as they really do, and every process that
    starts is recorded in ``calls``.
    """

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, prs: list[dict[str, Any]], current: int
    ) -> None:
        self.prs = prs
        self.calls: list[list[str]] = []
        self.lists: list[tuple[str, str]] = []
        self.tokens: list[str | None] = []
        self.token_lookups = 0
        # Whether the chosen account's token can be read.
        self.token_readable = True
        # Branches whose list fails, or runs past the walk's deadline.
        self.failing: set[str] = set()
        self.timing_out: set[str] = set()
        self.default_branch: str | None = "main"
        # Whether ``gh auth status`` takes ``--json``; an older ``gh`` doesn't.
        self.status_json = True
        self.deadlines: list[float | None] = []
        self.current = next(pr for pr in prs if pr["number"] == current)
        # The real token lookup runs, for an account the workspace chose.
        monkeypatch.setattr(github, "_in_sandbox", lambda: False)
        monkeypatch.setattr(github._config, "github_account_preference", lambda _repo: "octocat")
        monkeypatch.setattr(github, "_run", self._run)

    def _run(
        self, argv: list[str], *, cwd: str, timeout: float, env: dict[str, str] | None = None
    ) -> tuple[int | None, str, str]:
        assert argv[0] == "gh", argv
        argv = argv[1:]
        self.calls.append(argv)
        # Every process runs under the walk's one deadline.
        deadline = github._gh_deadline.get()
        assert deadline is not None
        assert timeout <= github_stack.WALK_SECONDS
        self.deadlines.append(deadline)
        if argv[:2] == ["auth", "token"]:
            self.token_lookups += 1
            if not self.token_readable:
                return 1, "", "no oauth token found for github.com account octocat"
            return 0, TOKEN + "\n", ""
        if argv[:3] == ["auth", "status", "--json"]:
            if not self.status_json:
                return 1, "", "unknown flag: --json"
            # The global listing shows github.com only, as for a host gh signs in per command.
            listing = {"hosts": {"github.com": [{"login": "octocat", "state": "success"}]}}
            return 0, json.dumps(listing), ""
        if argv[:2] == ["auth", "status"]:
            return 0, "", ""
        self.tokens.append((env or {}).get("GH_TOKEN"))
        if argv[:2] == ["pr", "view"]:
            return 0, json.dumps(self.current), ""
        if argv[:2] == ["repo", "view"]:
            if self.default_branch is None:
                return 1, "", "HTTP 502"
            return 0, json.dumps({"defaultBranchRef": {"name": self.default_branch}}), ""
        assert argv[:2] == ["pr", "list"], argv
        # Every list is of this repository, closed and merged PRs included, bounded.
        assert argv[argv.index("-R") + 1].endswith("example-org/example-repo"), argv
        assert argv[argv.index("--state") + 1] == "all", argv
        assert argv[argv.index("--limit") + 1] == str(github_stack._LIST_LIMIT), argv
        flag = "--head" if "--head" in argv else "--base"
        branch = argv[argv.index(flag) + 1]
        self.lists.append((flag, branch))
        if branch in self.timing_out:
            github._gh_timed_out.set(True)
            return 1, "", "deadline"
        if branch in self.failing:
            return 1, "", "HTTP 502"
        field = "headRefName" if flag == "--head" else "baseRefName"
        return 0, json.dumps([pr for pr in self.prs if pr[field] == branch]), ""


def _reference(number: int = 1, host: str = "github.com") -> PullRequestRef:
    url = PR_URL.format(number=number).replace("github.com", host, 1)
    return PullRequestRef.from_url(url)


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
    graph = StackGraph(monkeypatch, _chain(4), current=2)

    stack = github_stack.pull_request_stack("/workspace", _reference(2))

    assert _numbers(stack) == [1, 2, 3, 4]
    assert len(graph.calls) <= github_stack.MAX_GH_CALLS
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

    # The pull request's read and every list share one lookup.
    assert graph.token_lookups == 1
    assert len(graph.tokens) > 1 and set(graph.tokens) == {TOKEN}


def test_with_the_account_s_token_unreadable_every_call_runs_as_the_active_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = StackGraph(monkeypatch, _chain(4), current=2)
    graph.token_readable = False

    stack = github_stack.pull_request_stack("/workspace", _reference(2))

    assert _numbers(stack) == [1, 2, 3, 4]
    assert graph.token_lookups == 1
    assert len(graph.tokens) > 1 and set(graph.tokens) == {None}


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


def test_the_whole_read_stays_within_the_gh_call_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_stack, "MAX_DEPTH", 50)
    graph = StackGraph(monkeypatch, _chain(github_stack.MAX_GH_CALLS + 5), current=1)

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert stack["truncated"] is True
    # The account lookup and the reads before the walk count too.
    assert graph.calls[0][:2] == ["auth", "token"]
    assert len(graph.calls) <= github_stack.MAX_GH_CALLS


@pytest.mark.parametrize(
    "status_json", [True, False], ids=["listing-without-the-host", "gh-without-json"]
)
def test_a_read_on_an_enterprise_host_stays_within_the_gh_call_cap(
    monkeypatch: pytest.MonkeyPatch, status_json: bool
) -> None:
    """The sign-in check there can ask for the listing, its fallback, then the host."""
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "github.example.com")
    monkeypatch.setattr(github_stack, "MAX_DEPTH", 50)
    graph = StackGraph(monkeypatch, _chain(github_stack.MAX_GH_CALLS + 5), current=1)
    graph.status_json = status_json

    stack = github_stack.pull_request_stack("/workspace", _reference(1, "github.example.com"))

    assert stack["truncated"] is True
    assert ["auth", "status", "-h", "github.example.com"] in graph.calls
    assert (["auth", "status"] in graph.calls) is not status_json
    assert len(graph.calls) == github_stack.MAX_GH_CALLS


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
    # A contributor's pull request from their own repository's ``base-work`` branch.
    prs = [
        _pr(1, "branch-1", "base-work"),
        _pr(2, "base-work", "main", cross_repository=True),
    ]
    graph = StackGraph(monkeypatch, prs, current=1)

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert ("--head", "base-work") in graph.lists
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


def test_a_pull_request_from_another_repository_s_default_branch_still_has_its_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The contributor's main is not this repository's default branch.
    prs = [_pr(1, "feature", "main"), _pr(2, "main", "feature", cross_repository=True)]
    graph = StackGraph(monkeypatch, prs, current=2)

    stack = github_stack.pull_request_stack("/workspace", _reference(2))

    assert _numbers(stack) == [1, 2]
    assert stack["truncated"] is False
    assert ("--base", "main") not in graph.lists


def test_a_pull_request_github_cant_load_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = StackGraph(monkeypatch, _chain(2), current=1)
    monkeypatch.setattr(github, "_pr_json", lambda _root, _reference, _fields: None)

    with pytest.raises(ValueError) as refused:
        github_stack.pull_request_stack("/workspace", _reference(1))

    assert str(refused.value) == "GitHub could not load this pull request. Refresh to retry."
    assert graph.lists == []


def test_a_pull_request_from_the_default_branch_has_no_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A backport from main into a release branch, and the release branch merged back.
    prs = [_pr(1, "main", "release"), _pr(2, "release", "main")]
    graph = StackGraph(monkeypatch, prs, current=1)

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert _numbers(stack) == [1]
    assert stack["truncated"] is False
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


def test_without_the_default_branch_only_the_pull_request_is_listed_as_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without the default branch, a release PR from main would look like the parent.
    prs = [_pr(1, "feature", "main"), _pr(2, "main", "release"), _pr(3, "part-2", "feature")]
    graph = StackGraph(monkeypatch, prs, current=1)
    graph.default_branch = None

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert _numbers(stack) == [1]
    assert stack["truncated"] is True
    assert graph.lists == []


def test_a_contributors_pull_request_built_on_a_branch_here_is_in_the_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pull request 2 comes from a contributor's repository but targets this one's branch-1.
    prs = [*_chain(1), _pr(2, "their-branch", "branch-1", cross_repository=True)]
    graph = StackGraph(monkeypatch, prs, current=1)

    stack = github_stack.pull_request_stack("/workspace", _reference(1))

    assert _numbers(stack) == [1, 2]
    assert ("--base", "their-branch") not in graph.lists
