"""Searching GitHub for the session's pull request: reviewers, mentions and references.

Only the ``gh`` process is faked, so the host check, the token lookup and the GraphQL
request ``gh api`` is given run as they really do.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import github_resource as github
from omnigent.runner import github_search
from omnigent.runner.pull_request_errors import InvalidPullRequestInput, PullRequestError
from omnigent.runner.session_prs import PullRequestRef

REPOSITORY = "example-org/example-repo"
PR_URL = f"https://github.com/{REPOSITORY}/pull/7"
GHES = "code.example.com"
TOKEN = "ghp_example_token_value"


class FakeGraphQL:
    """Answers ``gh api graphql`` with ``answer``, and the repository's team list with
    ``teams``, recording each query and its variables.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[list[str]] = []
        self.requests: list[dict[str, Any]] = []
        self.envs: list[dict[str, str] | None] = []
        # A repository with no people and a search with no results, unless a test says.
        self.answer: Any = {
            "data": {"repository": {"assignableUsers": {"nodes": []}}, "search": {"nodes": []}}
        }
        self.raw_output: str | None = None
        self.exit_code = 0
        self.token_exit_code = 0
        # The teams with access to the repository, or ``None`` when GitHub refuses the list.
        self.teams: list[dict[str, Any]] | None = []

        def run(
            argv: list[str], *, cwd: str, timeout: float, env: dict[str, str] | None = None
        ) -> tuple[int | None, str, str]:
            assert argv[0] == "gh", argv
            self.calls.append(argv[1:])
            self.envs.append(env)
            if argv[1:3] == ["auth", "token"]:
                if self.token_exit_code:
                    return self.token_exit_code, "", "no oauth token found for octocat"
                return 0, TOKEN + "\n", ""
            assert argv[1] == "api", argv
            if argv[-1].startswith(f"repos/{REPOSITORY}/teams"):
                if self.teams is None:
                    return 1, '{"message": "Not Found"}', "gh: Not Found (HTTP 404)"
                return 0, json.dumps(self.teams), ""
            assert "graphql" in argv, argv
            path = argv[argv.index("--input") + 1]
            self.requests.append(json.loads(Path(path).read_text(encoding="utf-8")))
            if self.raw_output is not None:
                return self.exit_code, self.raw_output, "gh: example-org/secret-repo failed"
            return self.exit_code, json.dumps(self.answer), "gh: example-org/secret-repo failed"

        monkeypatch.setattr(github, "_run", run)

    def api_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "api"]


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


def _reference(url: str = PR_URL) -> PullRequestRef:
    return PullRequestRef.from_url(url)


def _people(*logins: str) -> dict[str, Any]:
    return {"nodes": [{"login": login, "name": login.title()} for login in logins]}


def _people_named(prefix: str, count: int) -> dict[str, Any]:
    return {"nodes": [{"login": f"{prefix}-{index}", "name": None} for index in range(count)]}


def test_reviewers_are_the_repositorys_people_then_its_teams_with_access(
    tmp_path: Path, fake: FakeGraphQL
) -> None:
    fake.answer = {"data": {"repository": {"assignableUsers": _people("alice", "alvaro")}}}
    fake.teams = [
        {"slug": "alpha-reviewers", "name": "Reviewers"},
        {"slug": "platform", "name": "Allied platform"},
        {"slug": "docs", "name": "Docs"},
    ]

    found = github_search.search_reviewers(str(tmp_path), _reference(), text="Al", page_size=5)

    assert found == {
        "object": "list",
        "data": [
            {"login": "alice", "kind": "user", "name": "Alice"},
            {"login": "alvaro", "kind": "user", "name": "Alvaro"},
            # Matched by slug, then by name; a team that matches neither is left out.
            {"login": "alpha-reviewers", "kind": "team", "name": "Reviewers"},
            {"login": "platform", "kind": "team", "name": "Allied platform"},
        ],
        "has_more": False,
    }
    (request,) = fake.requests
    # The text is a variable, never part of the query.
    assert request["variables"] == {
        "owner": "example-org",
        "name": "example-repo",
        "q": "Al",
        # One past the page, to tell a full page of people from a cut one.
        "first": 6,
    }
    assert "Al" not in request["query"]
    # Only teams with access to the repository are offered, never the whole organization's.
    assert "organization" not in request["query"]
    assert fake.api_calls()[1][-1] == f"repos/{REPOSITORY}/teams?per_page=100"
    for call in fake.api_calls():
        assert call[call.index("--hostname") + 1] == "github.com"


@pytest.mark.parametrize(
    ("page_size", "people", "teams", "shown"),
    [
        (3, 0, 10, ["team-0", "team-1", "team-2"]),
        (4, 3, 3, ["user-0", "user-1", "user-2", "team-0"]),
        (2, 3, 0, ["user-0", "user-1"]),
    ],
    ids=["teams-only", "people-then-teams", "people-only"],
)
def test_the_page_holds_people_then_teams_up_to_its_size(
    tmp_path: Path, fake: FakeGraphQL, page_size: int, people: int, teams: int, shown: list[str]
) -> None:
    fake.answer = {"data": {"repository": {"assignableUsers": _people_named("user", people)}}}
    fake.teams = [{"slug": f"team-{index}", "name": None} for index in range(teams)]

    found = github_search.search_reviewers(
        str(tmp_path), _reference(), text="", page_size=page_size
    )

    assert [entry["login"] for entry in found["data"]] == shown
    assert found["has_more"] is True


def test_with_no_page_size_the_page_holds_the_most_in_all(
    tmp_path: Path, fake: FakeGraphQL
) -> None:
    fake.answer = {"data": {"repository": {"assignableUsers": _people_named("user", 21)}}}
    fake.teams = [{"slug": f"team-{index}", "name": None} for index in range(10)]

    found = github_search.search_reviewers(str(tmp_path), _reference(), text="", page_size=0)

    assert len(found["data"]) == github_search.MAX_REVIEWERS
    assert {entry["kind"] for entry in found["data"]} == {"user"}
    assert found["has_more"] is True
    assert fake.requests[0]["variables"]["first"] == github_search.MAX_REVIEWERS + 1


def test_a_team_list_cut_at_its_cap_says_there_may_be_more(
    tmp_path: Path, fake: FakeGraphQL
) -> None:
    """GitHub lists 100 teams at most here, so a matching team past them can't be offered."""
    fake.teams = [{"slug": f"team-{index}", "name": None} for index in range(100)]

    found = github_search.search_reviewers(
        str(tmp_path), _reference(), text="team-9", page_size=20
    )

    assert [entry["login"] for entry in found["data"]][:2] == ["team-9", "team-90"]
    assert found["has_more"] is True


def test_a_repository_whose_teams_cant_be_listed_answers_its_people(
    tmp_path: Path, fake: FakeGraphQL
) -> None:
    """A personal repository has no teams, and listing them may need more access."""
    fake.answer = {"data": {"repository": {"assignableUsers": _people("alice")}}}
    fake.teams = None

    found = github_search.search_reviewers(str(tmp_path), _reference(), text="a", page_size=0)

    assert found["data"] == [{"login": "alice", "kind": "user", "name": "Alice"}]


@pytest.mark.parametrize("page_size", [-1, 21])
def test_a_page_size_out_of_range_is_refused_with_no_gh_call(
    tmp_path: Path, fake: FakeGraphQL, page_size: int
) -> None:
    with pytest.raises(InvalidPullRequestInput, match="at most 20 reviewers"):
        github_search.search_reviewers(str(tmp_path), _reference(), text="", page_size=page_size)

    assert fake.calls == []


def test_a_search_runs_as_the_account_saved_for_the_repository(
    tmp_path: Path, fake: FakeGraphQL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github._config, "github_account_preference", lambda _key: "octocat")

    github_search.search_reviewers(str(tmp_path), _reference(), text="a", page_size=0)

    # The token is read once for both reads.
    assert [call for call in fake.calls if call[:2] == ["auth", "token"]] == [
        ["auth", "token", "--user", "octocat", "-h", "github.com"]
    ]
    api_envs = [env for call, env in zip(fake.calls, fake.envs, strict=True) if call[0] == "api"]
    assert len(api_envs) == 2
    assert all(env is not None and env["GH_TOKEN"] == TOKEN for env in api_envs)


def test_a_saved_account_whose_token_cant_be_read_searches_as_ghs_active_one(
    tmp_path: Path, fake: FakeGraphQL, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As the reviewer picker's read: a read falls back rather than fail outright."""
    monkeypatch.setattr(github._config, "github_account_preference", lambda _key: "octocat")
    fake.token_exit_code = 1

    github_search.search_reviewers(str(tmp_path), _reference(), text="a", page_size=0)

    api_envs = [env for call, env in zip(fake.calls, fake.envs, strict=True) if call[0] == "api"]
    assert len(api_envs) == 2
    assert all(env is None or "GH_TOKEN" not in env for env in api_envs)


def test_a_pr_on_a_host_never_set_up_runs_no_gh_with_an_enterprise_token(
    tmp_path: Path, fake: FakeGraphQL, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh would treat every enterprise host as signed in and send it this token."""
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", TOKEN)
    reference = _reference(f"https://{GHES}/{REPOSITORY}/pull/7")

    with pytest.raises(PullRequestError, match="Sign in to this GitHub host"):
        github_search.search_reviewers(str(tmp_path), reference, text="a", page_size=0)
    with pytest.raises(PullRequestError, match="Sign in to this GitHub host"):
        github_search.search_completions(str(tmp_path), reference, kind="REFERENCE", text="")

    assert fake.calls == []


def test_a_pr_on_the_gh_host_set_up_is_searched_with_no_sign_in_probe(
    tmp_path: Path, fake: FakeGraphQL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_HOST", GHES)

    github_search.search_reviewers(
        str(tmp_path), _reference(f"https://{GHES}/{REPOSITORY}/pull/7"), text="a", page_size=0
    )

    # The two reads, and no sign-in probe before them.
    assert [call[0] for call in fake.calls] == ["api", "api"]
    for call in fake.calls:
        assert call[call.index("--hostname") + 1] == GHES


@pytest.mark.parametrize("output", ["<html>example-org/secret-repo</html>", "[]", '{"x": 1}'])
def test_an_answer_that_isnt_data_names_nothing_gh_said(
    tmp_path: Path, fake: FakeGraphQL, output: str
) -> None:
    fake.raw_output = output

    with pytest.raises(PullRequestError) as failed:
        github_search.search_reviewers(str(tmp_path), _reference(), text="a", page_size=0)

    assert str(failed.value) == "GitHub couldn't answer this search. Try again."


_SECRET_ERROR = {
    "message": "Could not resolve to a Repository with the name 'example-org/secret-repo'."
}


@pytest.mark.parametrize(
    "answer",
    [
        # The repository lookup failed: gh exits nonzero and GitHub names the repository.
        {"data": {"repository": None}, "errors": [_SECRET_ERROR]},
        {"data": None, "errors": [_SECRET_ERROR]},
        # The repository answered, but without its people.
        {"data": {"repository": {"assignableUsers": None}}},
        {"data": {"repository": {}}},
    ],
    ids=["null-repository", "errors-only", "null-people", "no-people"],
)
@pytest.mark.parametrize("search", ["reviewers", "mention"])
def test_a_repository_that_cant_be_read_is_a_failure_not_no_matches(
    tmp_path: Path, fake: FakeGraphQL, answer: dict[str, Any], search: str
) -> None:
    """Lost access or a missing repository must not read as a search with no matches."""
    fake.exit_code = 1
    fake.answer = answer

    with pytest.raises(PullRequestError) as failed:
        if search == "reviewers":
            github_search.search_reviewers(str(tmp_path), _reference(), text="a", page_size=0)
        else:
            github_search.search_completions(str(tmp_path), _reference(), kind="MENTION", text="a")

    assert str(failed.value) == "GitHub couldn't answer this search. Try again."


@pytest.mark.parametrize(
    "answer",
    [
        {"data": {"search": None}, "errors": [_SECRET_ERROR]},
        {"data": None, "errors": [_SECRET_ERROR]},
    ],
    ids=["null-search", "errors-only"],
)
def test_a_reference_search_github_failed_is_a_failure_not_no_matches(
    tmp_path: Path, fake: FakeGraphQL, answer: dict[str, Any]
) -> None:
    fake.exit_code = 1
    fake.answer = answer

    with pytest.raises(PullRequestError) as failed:
        github_search.search_completions(str(tmp_path), _reference(), kind="REFERENCE", text="")

    assert str(failed.value) == "GitHub couldn't answer this search. Try again."


def test_mention_completions_are_people_only(tmp_path: Path, fake: FakeGraphQL) -> None:
    fake.answer = {"data": {"repository": {"assignableUsers": _people("alice")}}}

    found = github_search.search_completions(
        str(tmp_path), _reference(), kind="MENTION", text="al"
    )

    assert found == {
        "object": "list",
        "kind": "mention",
        "people": [{"login": "alice", "kind": "user", "name": "Alice"}],
    }
    (request,) = fake.requests
    assert "organization" not in request["query"]
    assert request["variables"]["first"] == github_search.MAX_COMPLETIONS


def _pull_request(number: int, **fields: Any) -> dict[str, Any]:
    return {
        "url": f"https://github.com/{REPOSITORY}/pull/{number}",
        "number": number,
        "title": f"PR {number}",
        "state": "OPEN",
        "isDraft": False,
        **fields,
    }


def test_reference_completions_search_the_repositorys_pull_requests_by_quoted_words(
    tmp_path: Path, fake: FakeGraphQL
) -> None:
    fake.answer = {"data": {"search": {"nodes": [_pull_request(12), {}]}}}

    found = github_search.search_completions(
        str(tmp_path), _reference(), kind="REFERENCE", text="fix  login"
    )

    assert found == {
        "object": "list",
        "kind": "reference",
        "pull_requests": [
            {
                "url": f"https://github.com/{REPOSITORY}/pull/12",
                "number": 12,
                "title": "PR 12",
                "state": "OPEN",
                "is_draft": False,
            }
        ],
    }
    assert fake.requests[0]["variables"] == {
        "q": f'is:pr repo:{REPOSITORY} "fix" "login"',
        "first": github_search.MAX_COMPLETIONS,
    }


@pytest.mark.parametrize(
    "node",
    [
        _pull_request(1, url="https://github.com/example-org/other-repo/pull/1"),
        _pull_request(2, url=f"https://{GHES}/{REPOSITORY}/pull/2"),
        _pull_request(3, url="javascript:alert(1)"),
        _pull_request(4, url=None),
    ],
    ids=["other-repository", "other-host", "not-a-url", "no-url"],
)
def test_a_reference_outside_the_repository_is_dropped(
    tmp_path: Path, fake: FakeGraphQL, node: dict[str, Any]
) -> None:
    """What GitHub answers is untrusted: only the repository's own pull requests are offered."""
    fake.answer = {"data": {"search": {"nodes": [node]}}}

    found = github_search.search_completions(
        str(tmp_path), _reference(), kind="REFERENCE", text=""
    )

    assert found["pull_requests"] == []


def test_a_reference_title_that_isnt_text_is_shown_empty(
    tmp_path: Path, fake: FakeGraphQL
) -> None:
    fake.answer = {"data": {"search": {"nodes": [_pull_request(5, title=["<b>x</b>"])]}}}

    (found,) = github_search.search_completions(
        str(tmp_path), _reference(), kind="REFERENCE", text=""
    )["pull_requests"]

    assert found["title"] == ""


@pytest.mark.parametrize("text", ["is:open", "-label", 'say "hi"', "fix OR break"])
def test_search_syntax_in_a_reference_search_is_refused_with_no_gh_call(
    tmp_path: Path, fake: FakeGraphQL, text: str
) -> None:
    with pytest.raises(InvalidPullRequestInput, match="without search qualifiers"):
        github_search.search_completions(str(tmp_path), _reference(), kind="REFERENCE", text=text)

    assert fake.calls == []


def test_a_completion_of_no_known_kind_is_refused(tmp_path: Path, fake: FakeGraphQL) -> None:
    with pytest.raises(InvalidPullRequestInput, match="Choose what to complete"):
        github_search.search_completions(str(tmp_path), _reference(), kind="UNSPECIFIED", text="")

    assert fake.calls == []
