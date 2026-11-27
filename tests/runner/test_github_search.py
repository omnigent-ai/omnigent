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
        self.answer: Any = {"data": {}}
        self.raw_output: str | None = None
        self.exit_code = 0
        # The teams with access to the repository, or ``None`` when GitHub refuses the list.
        self.teams: list[dict[str, Any]] | None = []

        def run(
            argv: list[str], *, cwd: str, timeout: float, env: dict[str, str] | None = None
        ) -> tuple[int | None, str, str]:
            assert argv[0] == "gh", argv
            self.calls.append(argv[1:])
            self.envs.append(env)
            if argv[1:3] == ["auth", "token"]:
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

    def graphql_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "api" and "graphql" in call]

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
    }
    (request,) = fake.requests
    # The text is a variable, never part of the query.
    assert request["variables"] == {
        "owner": "example-org",
        "name": "example-repo",
        "q": "Al",
        "first": 5,
    }
    assert "Al" not in request["query"]
    # Only teams with access to the repository are offered, never the whole organization's.
    assert "organization" not in request["query"]
    assert fake.api_calls()[1][-1] == f"repos/{REPOSITORY}/teams?per_page=100"
    for call in fake.api_calls():
        assert call[call.index("--hostname") + 1] == "github.com"


def test_teams_are_capped_at_the_page_size(tmp_path: Path, fake: FakeGraphQL) -> None:
    fake.teams = [{"slug": f"team-{index}", "name": None} for index in range(10)]

    found = github_search.search_reviewers(str(tmp_path), _reference(), text="", page_size=3)

    assert [entry["login"] for entry in found["data"]] == ["team-0", "team-1", "team-2"]


def test_reviewers_with_no_page_size_ask_for_the_most(tmp_path: Path, fake: FakeGraphQL) -> None:
    github_search.search_reviewers(str(tmp_path), _reference(), text="", page_size=0)

    assert fake.requests[0]["variables"]["first"] == github_search.MAX_REVIEWERS


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

    assert fake.calls[0] == ["auth", "token", "--user", "octocat", "-h", "github.com"]
    api_envs = [env for call, env in zip(fake.calls, fake.envs, strict=True) if call[0] == "api"]
    assert len(api_envs) == 2
    assert all(env is not None and env["GH_TOKEN"] == TOKEN for env in api_envs)


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
