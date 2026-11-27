"""Reading pull requests by URL beyond the session's: list stats and single lookups.

Only the ``gh`` process is faked, so the host check and the GraphQL request ``gh api``
is given run as they really do.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import github_lookup
from omnigent.runner import github_resource as github
from omnigent.runner.pull_request_errors import InvalidPullRequestInput, PullRequestError

REPOSITORY = "example-org/example-repo"
GHES = "code.example.com"
TOKEN = "ghp_example_token_value"
# Text GitHub or gh may write about a repository the caller never named.
LEAKED = "example-org/secret-repo"


def _url(number: int, host: str = "github.com", repository: str = REPOSITORY) -> str:
    return f"https://{host}/{repository}/pull/{number}"


class FakeGraphQL:
    """Answers ``gh api graphql`` with ``answer``, recording each request and environment."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[list[str]] = []
        self.requests: list[dict[str, Any]] = []
        self.envs: list[dict[str, str] | None] = []
        self.answer: Any = {"data": {}}
        self.raw_output: str | None = None
        self.exit_code = 0

        def run(
            argv: list[str], *, cwd: str, timeout: float, env: dict[str, str] | None = None
        ) -> tuple[int | None, str, str]:
            assert argv[0] == "gh", argv
            self.calls.append(argv[1:])
            self.envs.append(env)
            assert argv[1] == "api" and "graphql" in argv, argv
            path = argv[argv.index("--input") + 1]
            self.requests.append(json.loads(Path(path).read_text(encoding="utf-8")))
            output = self.raw_output if self.raw_output is not None else json.dumps(self.answer)
            return self.exit_code, output, f"gh: GraphQL: Could not resolve {LEAKED}"

        monkeypatch.setattr(github, "_run", run)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No stored or configured gh hosts, no GH_HOST, no saved account and no sandbox."""
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.delenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", raising=False)
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _key: None)
    yield


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGraphQL:
    return FakeGraphQL(monkeypatch)


def _stats_node(number: int, **fields: Any) -> dict[str, Any]:
    return {
        "url": _url(number),
        "additions": 10,
        "deletions": 2,
        "changedFiles": 3,
        "updatedAt": "2026-01-02T03:04:05Z",
        "headRefOid": "a" * 40,
        "baseRefOid": "b" * 40,
        "state": "OPEN",
        "isDraft": False,
        "commits": {
            "nodes": [{"commit": {"oid": "a" * 40, "statusCheckRollup": {"state": "FAILURE"}}}]
        },
        **fields,
    }


def test_list_stats_read_every_pull_request_in_one_query(
    tmp_path: Path, fake: FakeGraphQL
) -> None:
    fake.answer = {
        "data": {
            "viewer": {"login": "octocat"},
            "p0": {"pullRequest": _stats_node(7)},
            # Missing, or not visible to the account: left out, not a failure.
            "p1": {"pullRequest": None},
        }
    }

    found = github_lookup.pull_request_list_stats(str(tmp_path), pr_urls=[_url(7), _url(8)])

    assert found == {
        "object": "list",
        "reader": "octocat",
        "data": [
            {
                "url": _url(7),
                "repository": REPOSITORY,
                "number": 7,
                "additions": 10,
                "deletions": 2,
                "changed_files": 3,
                "updated_at": "2026-01-02T03:04:05Z",
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "state": "OPEN",
                "is_draft": False,
                "ci": "failing",
                "ci_head_sha": "a" * 40,
            }
        ],
    }
    (request,) = fake.requests
    # Each URL's parts are variables, never part of the query.
    assert request["variables"] == {
        "o0": "example-org",
        "n0": "example-repo",
        "k0": 7,
        "o1": "example-org",
        "n1": "example-repo",
        "k1": 8,
    }
    assert REPOSITORY not in request["query"]
    (call,) = fake.calls
    assert call[call.index("--hostname") + 1] == "github.com"


def test_list_stats_keep_the_pull_requests_github_answered_when_others_failed(
    tmp_path: Path, fake: FakeGraphQL
) -> None:
    """gh exits nonzero for a field error, but the data it read is still shown."""
    fake.exit_code = 1
    fake.answer = {
        "data": {
            "viewer": {"login": "octocat"},
            "p0": {"pullRequest": _stats_node(7)},
            "p1": None,
        },
        "errors": [{"message": f"Could not resolve to a Repository with the name '{LEAKED}'."}],
    }

    found = github_lookup.pull_request_list_stats(
        str(tmp_path), pr_urls=[_url(7), _url(1, repository=LEAKED)]
    )

    assert [entry["url"] for entry in found["data"]] == [_url(7)]
    assert found["reader"] == "octocat"
    assert "Could not resolve" not in json.dumps(found)


def _commits(state: str | None, *, oid: str = "c" * 40) -> dict[str, Any]:
    rollup = {"state": state} if state else None
    return {"nodes": [{"commit": {"oid": oid, "statusCheckRollup": rollup}}]}


@pytest.mark.parametrize(
    ("commits", "ci", "ci_head_sha"),
    [
        (_commits("SUCCESS"), "passing", "c" * 40),
        (_commits("PENDING"), "pending", "c" * 40),
        (_commits("EXPECTED"), "pending", "c" * 40),
        (_commits("ERROR"), "failing", "c" * 40),
        # Checks that never ran, an unknown state, no commit, and no commits at all.
        (_commits(None), None, "c" * 40),
        (_commits("NEW"), None, "c" * 40),
        ({"nodes": [{"commit": None}]}, None, None),
        ({"nodes": []}, None, None),
        (None, None, None),
    ],
    ids=[
        "success",
        "pending",
        "expected",
        "error",
        "no-rollup",
        "unknown-state",
        "no-commit",
        "no-commits",
        "null",
    ],
)
def test_list_stats_read_each_check_state(
    tmp_path: Path, fake: FakeGraphQL, commits: Any, ci: str | None, ci_head_sha: str | None
) -> None:
    fake.answer = {"data": {"p0": {"pullRequest": _stats_node(7, commits=commits)}}}

    (entry,) = github_lookup.pull_request_list_stats(str(tmp_path), pr_urls=[_url(7)])["data"]

    assert (entry["ci"], entry["ci_head_sha"]) == (ci, ci_head_sha)


def test_a_url_named_twice_is_read_once(tmp_path: Path, fake: FakeGraphQL) -> None:
    github_lookup.pull_request_list_stats(str(tmp_path), pr_urls=[_url(7), _url(7) + "/files"])

    assert fake.requests[0]["variables"] == {"o0": "example-org", "n0": "example-repo", "k0": 7}


@pytest.mark.parametrize(
    ("pr_urls", "refusal"),
    [
        ([], "Ask for 1 to 50 pull requests"),
        ([_url(number) for number in range(1, 52)], "Ask for 1 to 50 pull requests"),
        ([_url(7), "https://example.com/not/a/pull"], "Expected a GitHub pull request URL"),
        ([_url(7), _url(8, host=GHES)], "Ask for one code host's pull requests at a time"),
    ],
    ids=["none", "too-many", "not-a-pr", "mixed-hosts"],
)
def test_list_stats_that_cant_be_read_in_one_query_are_refused_with_no_gh_call(
    tmp_path: Path,
    fake: FakeGraphQL,
    monkeypatch: pytest.MonkeyPatch,
    pr_urls: list[str],
    refusal: str,
) -> None:
    monkeypatch.setenv("GH_HOST", GHES)

    with pytest.raises(InvalidPullRequestInput, match=refusal):
        github_lookup.pull_request_list_stats(str(tmp_path), pr_urls=pr_urls)

    assert fake.calls == []


def test_a_host_never_set_up_runs_no_gh_with_an_enterprise_token(
    tmp_path: Path, fake: FakeGraphQL, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh would treat every enterprise host as signed in and send it this token."""
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", TOKEN)

    with pytest.raises((PullRequestError, InvalidPullRequestInput)):
        github_lookup.pull_request_list_stats(str(tmp_path), pr_urls=[_url(7, host=GHES)])
    with pytest.raises((PullRequestError, InvalidPullRequestInput)):
        github_lookup.lookup_pull_request(str(tmp_path), pr_url=_url(7, host=GHES))

    assert fake.calls == []


def test_a_host_never_set_up_but_recognized_by_its_url_is_refused_before_gh(
    tmp_path: Path, fake: FakeGraphQL, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider can recognize a GitHub Enterprise URL the user never signed in to."""
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", TOKEN)
    monkeypatch.setattr(github, "_known_github_host", lambda host: host == "github.com")
    monkeypatch.setenv("GH_HOST", GHES)

    with pytest.raises(PullRequestError, match="Sign in to this GitHub host"):
        github_lookup.lookup_pull_request(str(tmp_path), pr_url=_url(7, host=GHES))
    with pytest.raises(PullRequestError, match="Sign in to this GitHub host"):
        github_lookup.pull_request_list_stats(str(tmp_path), pr_urls=[_url(7, host=GHES)])

    assert fake.calls == []


def test_the_gh_host_set_up_is_read_with_no_sign_in_probe(
    tmp_path: Path, fake: FakeGraphQL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_HOST", GHES)
    fake.answer = {"data": {"repository": {"pullRequest": {"url": _url(7, host=GHES)}}}}

    github_lookup.lookup_pull_request(str(tmp_path), pr_url=_url(7, host=GHES))

    # The one gh call is the read; no ``gh auth status -h`` ran first.
    (call,) = fake.calls
    assert call[call.index("--hostname") + 1] == GHES


def test_the_read_runs_as_ghs_active_account_never_one_saved_for_the_repository(
    tmp_path: Path, fake: FakeGraphQL, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL can name any repository, so no account chosen for one is used."""
    saved: list[str] = []

    def preference(key: str) -> str:
        saved.append(key)
        return "octocat"

    monkeypatch.setattr(github._config, "github_account_preference", preference)
    fake.answer = {"data": {"repository": {"pullRequest": {"url": _url(7)}}}}

    github_lookup.lookup_pull_request(str(tmp_path), pr_url=_url(7))
    github_lookup.pull_request_list_stats(str(tmp_path), pr_urls=[_url(7)])

    assert saved == []
    assert [call[0] for call in fake.calls] == ["api", "api"]
    # No GH_TOKEN is added: gh answers as its active account on the host.
    assert fake.envs == [None, None]


@pytest.mark.parametrize(
    "answer",
    [
        {"data": None, "errors": [{"message": f"Could not resolve to a Repository {LEAKED}"}]},
        {"errors": [{"message": LEAKED}]},
        f"<html>{LEAKED}</html>",
        [],
    ],
    ids=["errors-only", "no-data", "html", "list"],
)
def test_a_failed_read_says_nothing_github_or_gh_wrote(
    tmp_path: Path, fake: FakeGraphQL, answer: Any
) -> None:
    fake.exit_code = 1
    if isinstance(answer, str):
        fake.raw_output = answer
    else:
        fake.answer = answer

    for read in (
        lambda: github_lookup.lookup_pull_request(str(tmp_path), pr_url=_url(7)),
        lambda: github_lookup.pull_request_list_stats(str(tmp_path), pr_urls=[_url(7)]),
    ):
        with pytest.raises(PullRequestError) as failed:
            read()
        assert str(failed.value) == "GitHub couldn't read these pull requests. Try again."
        assert LEAKED not in str(failed.value)


def test_a_lookup_github_has_no_answer_for_says_nothing_github_wrote(
    tmp_path: Path, fake: FakeGraphQL
) -> None:
    """Missing and not visible read alike, and GitHub's own error names nothing."""
    fake.exit_code = 1
    fake.answer = {
        "data": {"repository": None},
        "errors": [{"message": f"Could not resolve to a Repository with the name '{LEAKED}'."}],
    }

    with pytest.raises(PullRequestError) as failed:
        github_lookup.lookup_pull_request(str(tmp_path), pr_url=_url(7))

    assert str(failed.value) == "GitHub has no such pull request, or this account can't see it."


def test_a_lookup_answers_the_pull_requests_title_and_state(
    tmp_path: Path, fake: FakeGraphQL
) -> None:
    fake.answer = {
        "data": {
            "repository": {
                "pullRequest": {
                    "url": _url(7),
                    "number": 7,
                    "title": "Fix the login",
                    "state": "MERGED",
                    "isDraft": False,
                    "updatedAt": "2026-01-02T03:04:05Z",
                }
            }
        }
    }

    found = github_lookup.lookup_pull_request(str(tmp_path), pr_url=_url(7) + "/files")

    assert found == {
        "object": "session.github.pull_request_lookup",
        "url": _url(7),
        "repository": REPOSITORY,
        "number": 7,
        "title": "Fix the login",
        "state": "MERGED",
        "is_draft": False,
        "updated_at": "2026-01-02T03:04:05Z",
    }
    assert fake.requests[0]["variables"] == {
        "owner": "example-org",
        "name": "example-repo",
        "number": 7,
    }


@pytest.mark.parametrize(
    "answered",
    [
        _url(8),
        _url(7, repository="example-org/other-repo"),
        _url(7, host="github.example.com"),
        "javascript:alert(1)",
        None,
        {"url": _url(7)},
    ],
    ids=["other-number", "other-repository", "other-host", "not-a-url", "none", "object"],
)
def test_githubs_url_is_kept_only_when_it_names_the_pull_request_asked_about(
    tmp_path: Path, fake: FakeGraphQL, answered: Any
) -> None:
    """What GitHub answers is untrusted: any other URL gives back the one asked about."""
    fake.answer = {
        "data": {
            "viewer": {"login": "octocat"},
            "repository": {"pullRequest": {"url": answered}},
            "p0": {"pullRequest": _stats_node(7, url=answered)},
        }
    }

    found = github_lookup.lookup_pull_request(str(tmp_path), pr_url=_url(7))
    (stats,) = github_lookup.pull_request_list_stats(str(tmp_path), pr_urls=[_url(7)])["data"]

    assert found["url"] == _url(7)
    assert stats["url"] == _url(7)


@pytest.mark.parametrize(
    "answered",
    ["https://GitHub.com/Example-Org/Example-Repo/pull/7", _url(7) + "/files", _url(7) + "/"],
    ids=["case", "files-tab", "trailing-slash"],
)
def test_githubs_url_for_the_same_pull_request_is_accepted_in_its_canonical_form(
    tmp_path: Path, fake: FakeGraphQL, answered: str
) -> None:
    fake.answer = {
        "data": {
            "repository": {"pullRequest": {"url": answered}},
            "p0": {"pullRequest": _stats_node(7, url=answered)},
        }
    }

    found = github_lookup.lookup_pull_request(str(tmp_path), pr_url=_url(7))
    (stats,) = github_lookup.pull_request_list_stats(str(tmp_path), pr_urls=[_url(7)])["data"]

    assert found["url"] == stats["url"] == _url(7)


def test_a_title_that_isnt_text_is_shown_empty(tmp_path: Path, fake: FakeGraphQL) -> None:
    fake.answer = {"data": {"repository": {"pullRequest": {"url": _url(7), "title": ["<b>"]}}}}

    found = github_lookup.lookup_pull_request(str(tmp_path), pr_url=_url(7))

    assert found["title"] == ""
