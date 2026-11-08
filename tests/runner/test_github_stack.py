"""Tests for :mod:`omnigent.runner.github_stack`, with ``gh`` answered by a fake GitHub."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import github_resource, github_stack
from omnigent.runner.session_prs import PullRequestRef

_PR = PullRequestRef.from_url("https://github.com/example/two/pull/42")
_HEAD = "a" * 40
_PARENT = "b" * 40
_BASE_COMMIT = "c" * 40
_OWN = "d" * 40
_COMMITS_ENDPOINT = "repos/example/two/pulls/42/commits?per_page=100"
_COMPARE = f"repos/example/two/compare/{_PARENT}...{_HEAD}"


class FakeGitHub:
    """Answer ``gh api`` calls by endpoint and record what was asked."""

    def __init__(
        self,
        *,
        pr: dict[str, Any],
        commits: list[str] | None = None,
        associated: dict[str, list[dict[str, Any]]] | None = None,
        pulls: list[dict[str, Any]] | None = None,
        compare: dict[str, Any] | None = None,
        graphql_repository: object = ...,
        push_after: str | None = None,
    ) -> None:
        self.pr = pr
        self.commits = commits or []
        self.associated = associated or {}
        self.pulls = pulls or []
        self.compare = compare or {"files": []}
        # Overrides the GraphQL ``repository`` answer, e.g. ``None`` for an unreadable fork.
        self.graphql_repository = graphql_repository
        # Moves the PR head once this endpoint has been read, as a push mid-request would.
        self.push_after = push_after
        self.graphql: list[dict[str, Any]] = []
        self.endpoints: list[str] = []

    def __call__(self, argv: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if "--input" in argv:
            fields = json.loads(Path(argv[argv.index("--input") + 1]).read_text())
            self.graphql.append(fields)
            variables = fields["variables"]
            found = {
                key: {"associatedPullRequests": {"nodes": self.associated.get(sha, [])}}
                for key, sha in variables.items()
                if key.startswith("c")
            }
            if self.graphql_repository is not ...:
                found = self.graphql_repository
            return 0, json.dumps({"data": {"repository": found}}), ""
        endpoint = argv[-1]
        self.endpoints.append(endpoint)
        if endpoint == self.push_after:
            self.pr = {**self.pr, "head": {**self.pr["head"], "sha": "9" * 40}}
        if endpoint == "repos/example/two/pulls/42":
            return 0, json.dumps(self.pr), ""
        if endpoint == _COMMITS_ENDPOINT:
            return 0, json.dumps([[{"sha": sha, "commit": {}} for sha in self.commits]]), ""
        if endpoint.startswith("repos/example/two/pulls?"):
            return 0, json.dumps(self.pulls), ""
        if endpoint.startswith("repos/example/two/compare/"):
            if "Accept: application/vnd.github.diff" in argv:
                return 0, "incremental-patch", ""
            return 0, json.dumps(self.compare), ""
        raise AssertionError(f"unexpected gh call: {argv}")


def _pr(
    *, base_ref: str = "main", head_repository: str | None = "fork/two", body: str = ""
) -> dict[str, Any]:
    return {
        "head": {
            "sha": _HEAD,
            "ref": "feature",
            "repo": {"full_name": head_repository} if head_repository else None,
        },
        "base": {"sha": "e" * 40, "ref": base_ref, "repo": {"default_branch": "main"}},
        "body": body,
    }


def _pull(number: int, head: str, *, state: str = "OPEN", base: str = "example/two") -> dict:
    return {
        "number": number,
        "url": f"https://github.com/example/two/pull/{number}",
        "title": f"Pull {number}",
        "state": state,
        "headRefOid": head,
        "baseRepository": {"nameWithOwner": base},
    }


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub) -> None:
    monkeypatch.setattr(github_resource, "_gh", fake)
    monkeypatch.setattr(github_resource, "_pr_token", lambda *_args: None)


def test_fork_stack_parent_is_newest_commit_heading_another_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The newest own commit that heads another PR in the base repository is the parent."""
    fake = FakeGitHub(
        pr=_pr(),
        commits=[_BASE_COMMIT, _PARENT, _OWN, _HEAD],
        associated={
            # The parent's commits belong to it and to every PR stacked above it.
            _BASE_COMMIT: [_pull(40, _BASE_COMMIT, state="MERGED"), _pull(42, _HEAD)],
            _PARENT: [_pull(41, _PARENT), _pull(42, _HEAD)],
            _OWN: [_pull(42, _HEAD)],
        },
    )
    _install(monkeypatch, fake)

    result = github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)

    assert result == {
        "object": "session.github.pr_stack",
        "head_sha": _HEAD,
        "parent": {
            "number": 41,
            "url": "https://github.com/example/two/pull/41",
            "title": "Pull 41",
            "state": "OPEN",
            "head_sha": _PARENT,
            "source": "commit",
        },
    }
    [request] = fake.graphql
    # Fork PRs are associated with their commits only in the fork, so the query asks it.
    assert request["variables"]["owner"] == "fork"
    assert request["variables"]["name"] == "two"
    assert [request["variables"][f"c{index}"] for index in range(3)] == [
        _OWN,
        _PARENT,
        _BASE_COMMIT,
    ]


def test_merged_parent_still_in_history_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(
        pr=_pr(head_repository=None),
        commits=[_PARENT, _HEAD],
        associated={_PARENT: [_pull(41, _PARENT, state="MERGED")]},
    )
    _install(monkeypatch, fake)

    parent = github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)["parent"]

    assert parent["number"] == 41
    assert parent["state"] == "MERGED"
    # A deleted fork falls back to the base repository.
    assert fake.graphql[0]["variables"]["owner"] == "example"


@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param(_pull(41, _PARENT, state="CLOSED"), id="closed"),
        pytest.param(_pull(41, _PARENT, base="other/two"), id="other-repository"),
        pytest.param(_pull(41, "f" * 40), id="commit-is-not-its-head"),
    ],
)
def test_commit_walk_ignores_pull_requests_that_are_not_parents(
    monkeypatch: pytest.MonkeyPatch, candidate: dict[str, Any]
) -> None:
    fake = FakeGitHub(pr=_pr(), commits=[_PARENT, _HEAD], associated={_PARENT: [candidate]})
    _install(monkeypatch, fake)

    assert github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)["parent"] is None


def test_single_commit_pr_has_no_parent_without_a_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(pr=_pr(), commits=[_HEAD])
    _install(monkeypatch, fake)

    assert github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)["parent"] is None
    assert fake.graphql == []


def test_commit_walk_is_one_bounded_query(monkeypatch: pytest.MonkeyPatch) -> None:
    commits = [f"{index:040x}" for index in range(1, 150)] + [_HEAD]
    fake = FakeGitHub(pr=_pr(), commits=commits)
    _install(monkeypatch, fake)

    github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)

    [request] = fake.graphql
    assert len([key for key in request["variables"] if key.startswith("c")]) == 100
    assert request["variables"]["c0"] == commits[-2]


def test_commit_list_that_stops_before_the_head_is_not_walked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub lists only a long PR's oldest commits, which may omit the nearest parent."""
    grandparent = "f" * 40
    oldest = [f"{index:040x}" for index in range(1, 250)] + [grandparent]
    fake = FakeGitHub(pr=_pr(), commits=oldest, associated={grandparent: [_pull(40, grandparent)]})
    _install(monkeypatch, fake)

    assert github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)["parent"] is None
    assert fake.graphql == []


def test_description_stack_link_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        "Adds the thing.\n\n## \N{PANCAKES} Stacked PR\n"
        f"- [Changes in this PR](https://github.com/example/two/pull/42/files/{_PARENT}..{_HEAD})\n"
    )
    fake = FakeGitHub(pr=_pr(body=body), commits=[_PARENT, _HEAD])
    _install(monkeypatch, fake)

    parent = github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)["parent"]

    assert parent == {
        "number": None,
        "url": None,
        "title": None,
        "state": None,
        "head_sha": _PARENT,
        "source": "description",
    }


def test_failed_commit_walk_falls_back_to_the_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable head repository still lets the description link name the parent."""
    body = f"## \N{PANCAKES} Stacked PR\n[diff](https://x/files/{_PARENT}..{_HEAD})\n"
    fake = FakeGitHub(pr=_pr(body=body), commits=[_PARENT, _HEAD], graphql_repository=None)
    _install(monkeypatch, fake)

    parent = github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)["parent"]

    assert (parent["head_sha"], parent["source"]) == (_PARENT, "description")


def test_failed_commit_walk_without_a_description_link_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGitHub(pr=_pr(), commits=[_PARENT, _HEAD], graphql_repository=None)
    _install(monkeypatch, fake)

    with pytest.raises(ValueError, match="could not look up the pull request's stack"):
        github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)


def test_description_link_outside_this_prs_commits_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = f"## \N{PANCAKES} Stacked PR\n[diff](https://x/files/{'f' * 40}..{_HEAD})\n"
    fake = FakeGitHub(pr=_pr(body=body), commits=[_PARENT, _HEAD])
    _install(monkeypatch, fake)

    assert github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)["parent"] is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(f"[diff](https://x/files/{_PARENT}..{_HEAD})", id="no-stack-section"),
        pytest.param(
            f"## \N{PANCAKES} Stacked PR\nnone\n## Notes\nhttps://x/files/{_PARENT}..{_HEAD}",
            id="link-in-a-later-section",
        ),
        pytest.param(None, id="no-body"),
    ],
)
def test_description_parent_requires_the_stack_section(body: str | None) -> None:
    assert github_stack.description_parent_sha(body) is None


def test_same_repository_stack_is_based_on_the_parent_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGitHub(
        pr=_pr(base_ref="parent-branch"),
        pulls=[
            {
                "number": 41,
                "html_url": "https://github.com/example/two/pull/41",
                "title": "Parent",
                "state": "open",
                "head": {
                    "sha": _PARENT,
                    "ref": "parent-branch",
                    "repo": {"full_name": "example/two"},
                },
            }
        ],
    )
    _install(monkeypatch, fake)

    parent = github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)["parent"]

    assert parent["number"] == 41
    assert parent["source"] == "base_branch"
    assert parent["state"] == "OPEN"
    assert "repos/example/two/pulls?state=open&per_page=5&head=example:parent-branch" in (
        fake.endpoints
    )
    assert fake.graphql == []


def test_pr_on_a_branch_no_open_pr_heads_has_no_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(pr=_pr(base_ref="release"))
    _install(monkeypatch, fake)

    assert github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)["parent"] is None


def test_stack_rejects_a_moved_head(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeGitHub(pr=_pr()))

    with pytest.raises(ValueError, match="pull request changed"):
        github_stack.pr_stack("/workspace", _PR, head_sha="f" * 40)


def test_stack_rejects_a_push_during_the_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commits read after a push belong to another head, so the answer is refused."""
    fake = FakeGitHub(pr=_pr(), commits=[_PARENT, _HEAD], push_after=_COMMITS_ENDPOINT)
    _install(monkeypatch, fake)

    with pytest.raises(ValueError, match="pull request changed"):
        github_stack.pr_stack("/workspace", _PR, head_sha=_HEAD)


def test_stack_diff_rejects_a_push_during_the_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGitHub(pr=_pr(), commits=[_PARENT, _HEAD], push_after=_COMMITS_ENDPOINT)
    _install(monkeypatch, fake)

    with pytest.raises(ValueError, match="pull request changed"):
        github_stack.pr_stack_diff("/workspace", _PR, head_sha=_HEAD, parent_sha=_PARENT)


def test_stack_diff_compares_parent_head_with_pr_head(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(
        pr=_pr(),
        commits=[_PARENT, _HEAD],
        compare={
            "files": [
                {"filename": "own.py", "status": "added", "additions": 3, "deletions": 0},
                {
                    "filename": "new_name.py",
                    "previous_filename": "old_name.py",
                    "status": "renamed",
                    "additions": 1,
                    "deletions": 1,
                },
            ]
        },
    )
    _install(monkeypatch, fake)

    result = github_stack.pr_stack_diff("/workspace", _PR, head_sha=_HEAD, parent_sha=_PARENT)

    assert result["object"] == "session.github.pr_stack_diff"
    assert result["patch"] == "incremental-patch"
    assert result["has_more"] is False
    assert (result["head_sha"], result["parent_sha"]) == (_HEAD, _PARENT)
    assert [(entry["path"], entry["status"]) for entry in result["files"]] == [
        ("own.py", "created"),
        ("new_name.py", "renamed"),
    ]
    assert result["files"][1]["previous_path"] == "old_name.py"
    assert fake.endpoints.count(_COMPARE) == 2


def test_stack_diff_flags_a_truncated_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    files = [{"filename": f"f{index}.py", "status": "modified"} for index in range(300)]
    _install(monkeypatch, FakeGitHub(pr=_pr(), commits=[_PARENT, _HEAD], compare={"files": files}))

    result = github_stack.pr_stack_diff("/workspace", _PR, head_sha=_HEAD, parent_sha=_PARENT)

    assert result["has_more"] is True
    assert "300 files" in result["warning"]


@pytest.mark.parametrize(
    ("parent_sha", "message"),
    [
        pytest.param("f" * 40, "not part of the selected pull request", id="foreign-commit"),
        pytest.param(_HEAD, "must differ", id="head-itself"),
    ],
)
def test_stack_diff_only_compares_within_the_pr(
    monkeypatch: pytest.MonkeyPatch, parent_sha: str, message: str
) -> None:
    fake = FakeGitHub(pr=_pr(), commits=[_PARENT, _HEAD])
    _install(monkeypatch, fake)

    with pytest.raises(ValueError, match=message):
        github_stack.pr_stack_diff("/workspace", _PR, head_sha=_HEAD, parent_sha=parent_sha)
    assert not any(
        endpoint.startswith("repos/example/two/compare/") for endpoint in fake.endpoints
    )
