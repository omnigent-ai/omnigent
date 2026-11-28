"""GitHub's own record of a pull request's stack: the read, and an answer it can't trust.

Only the ``gh`` process is faked, so the host check, the token lookup and the GraphQL
request ``gh api`` is given run as they really do.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.host.workspace_payloads import payload_dict, payload_struct
from omnigent.runner import github_native_stack
from omnigent.runner import github_resource as github
from omnigent.runner.pull_request_errors import PullRequestError
from omnigent.runner.session_prs import PullRequestRef

REPOSITORY = "example-org/example-repo"
PR_URL = f"https://github.com/{REPOSITORY}/pull/2"
GHES = "code.example.com"
TOKEN = "ghp_example_token_value"
UNREADABLE = "GitHub couldn't read this pull request's stack. Try again."
_NODES = ["repository", "pullRequest", "stack", "entries", "nodes"]


def _member(number: int, **fields: Any) -> dict[str, Any]:
    """A stack member as GitHub's GraphQL API answers one."""
    return {
        "url": f"https://github.com/{REPOSITORY}/pull/{number}",
        "number": number,
        "title": f"PR {number}",
        "state": "OPEN",
        "isDraft": False,
        "updatedAt": "2026-01-02T03:04:05Z",
        "reviewDecision": "REVIEW_REQUIRED",
        "headRefName": f"feature/part-{number}",
        "headRefOid": f"{number:040x}",
        "baseRefName": "main" if number == 1 else f"feature/part-{number - 1}",
        "baseRefOid": f"{number + 100:040x}",
        "headRepository": {"nameWithOwner": REPOSITORY},
        "baseRepository": {"nameWithOwner": REPOSITORY, "defaultBranchRef": {"name": "main"}},
        **fields,
    }


def _answer(
    nodes: list[Any] | None = None, *, has_next_page: bool = False, **stack: Any
) -> dict[str, Any]:
    """GitHub's answer for PR 2 in a stack of three, unless a test says otherwise."""
    if nodes is None:
        nodes = [{"position": number, "pullRequest": _member(number)} for number in (1, 2, 3)]
    return {
        "data": {
            "viewer": {"login": "octocat"},
            "repository": {
                "pullRequest": {
                    "number": 2,
                    "stackEntry": {"position": 2},
                    "stack": {
                        "id": "PRS_kwDOExample12",
                        "number": 4,
                        "size": 3,
                        "baseRefName": "main",
                        "entries": {"pageInfo": {"hasNextPage": has_next_page}, "nodes": nodes},
                        **stack,
                    },
                }
            },
        }
    }


def _shaped(number: int, **fields: Any) -> dict[str, Any]:
    """A member as the read answers it."""
    return {
        "url": f"https://github.com/{REPOSITORY}/pull/{number}",
        "number": number,
        "title": f"PR {number}",
        "state": "OPEN",
        "is_draft": False,
        "updated_at": "2026-01-02T03:04:05Z",
        "review_decision": "REVIEW_REQUIRED",
        "head_ref": f"feature/part-{number}",
        "head_sha": f"{number:040x}",
        "base_ref": "main" if number == 1 else f"feature/part-{number - 1}",
        "base_sha": f"{number + 100:040x}",
        "head_repository": REPOSITORY,
        "base_repository": REPOSITORY,
        "base_default_branch": "main",
        **fields,
    }


def _undefined_naming(type_name: Any, field: Any) -> dict[str, Any]:
    """An ``undefinedField`` error whose names aren't text."""
    return {"extensions": {"code": "undefinedField", "typeName": type_name, "fieldName": field}}


class FakeGraphQL:
    """Answers ``gh api graphql`` with ``answer``, recording each request and process."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.requests: list[dict[str, Any]] = []
        self.answer: Any = _answer()
        self.raw_output: str | None = None
        # gh exits 1 when GitHub's answer has errors, and still prints the answer.
        self.exit_code = 0

        def run(
            argv: list[str], *, cwd: str, timeout: float, env: dict[str, str] | None = None
        ) -> tuple[int | None, str, str]:
            assert argv[0] == "gh", argv
            self.calls.append(argv[1:])
            self.envs.append(env)
            if argv[1:3] == ["auth", "token"]:
                return 0, TOKEN + "\n", ""
            assert argv[1] == "api" and "graphql" in argv, argv
            path = argv[argv.index("--input") + 1]
            self.requests.append(json.loads(Path(path).read_text(encoding="utf-8")))
            output = self.raw_output if self.raw_output is not None else json.dumps(self.answer)
            return self.exit_code, output, "gh: example-org/secret-repo failed"

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


def _read(url: str = PR_URL) -> dict[str, Any]:
    return github_native_stack.pull_request_native_stack(
        "/workspace", PullRequestRef.from_url(url)
    )


def _entries(found: dict[str, Any]) -> list[dict[str, Any]]:
    return found["native_stack"]["stack"]["entries"]["nodes"]


def test_a_stacked_pull_request_reads_its_whole_stack(fake: FakeGraphQL) -> None:
    found = _read()

    assert found == {
        "object": "pull_request_native_stack",
        "supported": True,
        "viewer": "octocat",
        "native_stack": {
            "stackEntry": {"position": 2},
            "stack": {
                "id": "PRS_kwDOExample12",
                "number": 4,
                "size": 3,
                "baseRefName": "main",
                "entries": {
                    "hasNextPage": False,
                    "nodes": [
                        {"position": number, "pullRequest": _shaped(number)}
                        for number in (1, 2, 3)
                    ],
                },
            },
        },
    }
    (call,) = fake.api_calls()
    assert call[call.index("--hostname") + 1] == "github.com"
    (request,) = fake.requests
    # The request's repository and number go in as variables, never into the query.
    assert request["variables"] == {"owner": "example-org", "name": "example-repo", "number": 2}
    assert f"entries(first: {github_native_stack.MAX_NATIVE_STACK_ENTRIES})" in request["query"]


def test_an_answer_with_null_errors_reads_as_one_with_none(fake: FakeGraphQL) -> None:
    fake.answer["errors"] = None

    assert _entries(_read())[1] == {"position": 2, "pullRequest": _shaped(2)}


def test_the_largest_numbers_accepted_survive_the_payload_unchanged(fake: FakeGraphQL) -> None:
    """The answer travels as a Struct, whose numbers are doubles."""
    largest = 2**31 - 1
    url = f"https://github.com/{REPOSITORY}/pull/{largest}"
    fake.answer = _answer(
        [{"position": largest, "pullRequest": {**_member(3), "number": largest, "url": url}}],
        number=largest,
        size=10_000,
    )

    found = _read()

    assert payload_dict(payload_struct(found)) == found
    (entry,) = _entries(found)
    assert (entry["pullRequest"]["number"], entry["pullRequest"]["url"]) == (largest, url)
    assert found["native_stack"]["stack"]["number"] == largest


def test_a_pull_request_in_no_stack_says_so(fake: FakeGraphQL) -> None:
    pull_request = fake.answer["data"]["repository"]["pullRequest"]
    pull_request.update(stackEntry=None, stack=None)

    found = _read()

    assert found["native_stack"] == {"stackEntry": None, "stack": None}


@pytest.mark.parametrize(
    ("listed", "has_next_page"),
    [(50, True), (51, True), (51, False)],
    ids=["a-full-read", "more-than-asked-for", "more-than-asked-for-saying-none"],
)
def test_a_stack_longer_than_one_read_says_there_are_more(
    fake: FakeGraphQL, listed: int, has_next_page: bool
) -> None:
    """An answer with more entries than the read asked for is cut, and says it has more."""
    count = github_native_stack.MAX_NATIVE_STACK_ENTRIES
    nodes = [{"position": n, "pullRequest": _member(n)} for n in range(1, listed + 1)]
    fake.answer = _answer(nodes, has_next_page=has_next_page, size=count + 5)

    stack = _read()["native_stack"]["stack"]

    assert stack["entries"]["hasNextPage"] is True
    assert stack["size"] == count + 5
    assert [entry["position"] for entry in stack["entries"]["nodes"]] == list(range(1, count + 1))


@pytest.mark.parametrize(
    "url",
    [
        f"https://github.com/{REPOSITORY}/pull/3/files",
        "https://GitHub.com/Example-Org/Example-Repo/pull/3",
    ],
    ids=["subpage", "other-casing"],
)
def test_a_members_url_is_the_canonical_one(fake: FakeGraphQL, url: str) -> None:
    fake.answer = _answer([{"position": 3, "pullRequest": _member(3, url=url)}])

    (entry,) = _entries(_read())

    assert entry["pullRequest"]["url"] == f"https://github.com/{REPOSITORY}/pull/3"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example-org/other-repo/pull/3",
        f"https://{GHES}/{REPOSITORY}/pull/3",
    ],
    ids=["other-repository", "other-host"],
)
def test_a_member_outside_the_repository_is_blanked(fake: FakeGraphQL, url: str) -> None:
    """Nothing about a PR outside the requested repository crosses, but its gap is named."""
    fake.answer = _answer([{"position": 3, "pullRequest": _member(3, url=url)}])

    (entry,) = _entries(_read())

    assert entry == {"position": 3, "pullRequest": None, "outside_repository": True}


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "javascript:alert(1)"},
        {"url": None},
        {"number": 4},
        {"number": "3"},
        # Past GraphQL's Int, so the payload's doubles could change it; its URL agrees.
        {"number": 2**31, "url": f"https://github.com/{REPOSITORY}/pull/{2**31}"},
        {"state": "DELETED"},
        {"state": []},
        {"state": {}},
        {"isDraft": "no"},
        {"headRefName": ""},
        {"headRefName": "é" * 128},
        {"headRefName": "feature\nlogin"},
        {"headRefName": "feature/\ud800"},
    ],
    ids=[
        "not-a-pr-url",
        "no-url",
        "other-number",
        "number-as-text",
        "number-past-int",
        "unknown-state",
        "state-as-list",
        "state-as-object",
        "draft-as-text",
        "empty-head",
        "head-256-bytes",
        "head-control-character",
        "head-unpaired-surrogate",
    ],
)
def test_a_member_github_answered_wrongly_is_a_gap(
    fake: FakeGraphQL, fields: dict[str, Any]
) -> None:
    fake.answer = _answer([{"position": 3, "pullRequest": {**_member(3), **fields}}])

    (entry,) = _entries(_read())

    assert entry == {"position": 3, "pullRequest": None}


@pytest.mark.parametrize(
    ("fields", "shaped"),
    [
        ({"title": 7}, {"title": ""}),
        ({"title": "é" * 1025}, {"title": "é" * 1024}),
        ({"reviewDecision": "LGTM"}, {"review_decision": None}),
        ({"reviewDecision": {}}, {"review_decision": None}),
        ({"reviewDecision": []}, {"review_decision": None}),
        ({"title": "PR \ud800"}, {"title": ""}),
        ({"updatedAt": "\uff12\uff10\uff12\uff16-01-02T03:04:05Z"}, {"updated_at": None}),
        ({"updatedAt": "yesterday"}, {"updated_at": None}),
        ({"updatedAt": "2026-01-02 03:04:05"}, {"updated_at": None}),
        ({"updatedAt": "2026-13-45T25:61:61Z"}, {"updated_at": None}),
        ({"headRefOid": "A" * 40}, {"head_sha": None}),
        ({"baseRefOid": "abc"}, {"base_sha": None}),
        ({"baseRefName": "é" * 128}, {"base_ref": None}),
        ({"baseRefName": "\udfff"}, {"base_ref": None}),
        ({"headRepository": None}, {"head_repository": None}),
        (
            {"headRepository": {"nameWithOwner": "example-org/../secret"}},
            {"head_repository": None},
        ),
        (
            {"headRepository": {"nameWithOwner": "x" * 128 + "/" + "y" * 128}},
            {"head_repository": None},
        ),
        (
            {
                "baseRepository": {
                    "nameWithOwner": "no-slash",
                    "defaultBranchRef": {"name": "main"},
                }
            },
            {"base_repository": None},
        ),
        (
            {"baseRepository": {"nameWithOwner": REPOSITORY, "defaultBranchRef": None}},
            {"base_default_branch": None},
        ),
    ],
    ids=[
        "title-not-text",
        "title-past-cap",
        "unknown-review",
        "review-as-object",
        "review-as-list",
        "title-unpaired-surrogate",
        "updated-other-digits",
        "updated-not-a-time",
        "updated-not-iso",
        "updated-impossible-date",
        "head-sha-uppercase",
        "base-sha-short",
        "base-ref-256-bytes",
        "base-ref-unpaired-surrogate",
        "deleted-fork",
        "repository-dot-dot",
        "repository-257-characters",
        "repository-not-owner-repo",
        "no-default-branch",
    ],
)
def test_a_field_github_answered_wrongly_is_left_out(
    fake: FakeGraphQL, fields: dict[str, Any], shaped: dict[str, Any]
) -> None:
    fake.answer = _answer([{"position": 3, "pullRequest": _member(3, **fields)}])

    (entry,) = _entries(_read())

    assert entry == {"position": 3, "pullRequest": _shaped(3, **shaped)}


def test_fields_at_their_limits_are_kept(fake: FakeGraphQL) -> None:
    head = "é" * 127 + "x"
    repository = "x" * 128 + "/" + "y" * 127
    fake.answer = _answer(
        [
            {
                "position": 3,
                "pullRequest": _member(
                    3,
                    headRefName=head,
                    title="t" * 1024,
                    headRepository={"nameWithOwner": repository},
                ),
            }
        ]
    )

    (entry,) = _entries(_read())

    member = entry["pullRequest"]
    assert (member["head_ref"], member["title"], member["head_repository"]) == (
        head,
        "t" * 1024,
        repository,
    )


def test_repeated_and_out_of_range_positions_are_kept(fake: FakeGraphQL) -> None:
    """The page's model marks repeated or out-of-range positions; the host invents no order."""
    fake.answer = _answer(
        [
            {"position": 9, "pullRequest": _member(1)},
            {"position": 9, "pullRequest": _member(2)},
            {"position": 0, "pullRequest": _member(3)},
            {"position": 2**31 - 1, "pullRequest": _member(4)},
        ]
    )

    assert [entry["position"] for entry in _entries(_read())] == [9, 9, 0, 2**31 - 1]


@pytest.mark.parametrize("login", ["octocat", "github-actions[bot]", "example-app[bot]"])
def test_the_account_it_was_read_as_may_be_a_user_or_an_app(fake: FakeGraphQL, login: str) -> None:
    """A GitHub App's token reads as a login ending in ``[bot]``."""
    fake.answer["data"]["viewer"]["login"] = login

    assert _read()["viewer"] == login


def test_a_member_github_hides_is_a_gap(fake: FakeGraphQL) -> None:
    """The error decides, even where the answer still has usable data at that path."""
    nodes = [
        {"position": 1, "pullRequest": None},
        {"position": 2, "pullRequest": _member(2)},
        {"position": 3, "pullRequest": _member(3)},
    ]
    fake.answer = _answer(nodes)
    fake.answer["errors"] = [
        {"type": "FORBIDDEN", "path": [*_NODES, 0, "pullRequest"], "message": "secret-repo"},
        {"type": "NOT_FOUND", "path": [*_NODES, 2, "pullRequest"], "message": "secret-repo"},
    ]
    fake.exit_code = 1

    entries = _entries(_read())

    assert entries == [
        {"position": 1, "pullRequest": None},
        {"position": 2, "pullRequest": _shaped(2)},
        {"position": 3, "pullRequest": None},
    ]


@pytest.mark.parametrize(
    ("field", "shaped"),
    [
        (["headRepository"], {"head_repository": None}),
        (["headRepository", "nameWithOwner"], {"head_repository": None}),
        (["baseRepository"], {"base_repository": None, "base_default_branch": None}),
        (["baseRepository", "nameWithOwner"], {"base_repository": None}),
        (["baseRepository", "defaultBranchRef"], {"base_default_branch": None}),
        (["baseRepository", "defaultBranchRef", "name"], {"base_default_branch": None}),
    ],
    ids=[
        "private-fork",
        "private-fork-name",
        "base-repository",
        "base-repository-name",
        "default-branch",
        "default-branch-name",
    ],
)
def test_a_field_github_hides_inside_a_member_is_left_out_and_the_member_kept(
    fake: FakeGraphQL, field: list[str], shaped: dict[str, Any]
) -> None:
    """A private fork's head repository says nothing about the PR itself.

    The member keeps its usable values at the hidden path, so only the error can leave it out.
    """
    fake.answer = _answer([{"position": 3, "pullRequest": _member(3)}])
    fake.answer["errors"] = [
        {"type": "FORBIDDEN", "path": [*_NODES, 0, "pullRequest", *field], "message": "secret"}
    ]

    (entry,) = _entries(_read())

    assert entry == {"position": 3, "pullRequest": _shaped(3, **shaped)}


def test_a_required_field_github_hides_makes_its_member_a_gap(fake: FakeGraphQL) -> None:
    fake.answer = _answer([{"position": 3, "pullRequest": _member(3)}])
    fake.answer["errors"] = [
        {"type": "FORBIDDEN", "path": [*_NODES, 0, "pullRequest", "headRefName"], "message": "x"}
    ]

    (entry,) = _entries(_read())

    assert entry == {"position": 3, "pullRequest": None}


@pytest.mark.parametrize(
    "fields",
    [
        [("PullRequest", "stack"), ("PullRequest", "stackEntry")],
        [("PullRequestStack", "size")],
        [("PullRequestStackEntry", "position")],
    ],
    ids=["no-stack-fields", "no-stack-type-field", "no-entry-type-field"],
)
def test_a_host_without_stacks_cant_say(fake: FakeGraphQL, fields: list[tuple[str, str]]) -> None:
    fake.answer = {
        "errors": [
            {
                "extensions": {"code": "undefinedField", "typeName": type_name, "fieldName": name},
                "message": f"Field '{name}' doesn't exist on type '{type_name}'",
            }
            for type_name, name in fields
        ]
    }
    fake.exit_code = 1

    assert _read() == {"object": "pull_request_native_stack", "supported": False, "viewer": None}


@pytest.mark.parametrize(
    "errors",
    [
        # A message alone never says the host lacks stacks.
        [{"message": "Field 'stack' doesn't exist on type 'PullRequest'"}],
        [
            {
                "extensions": {
                    "code": "undefinedField",
                    "typeName": "PullRequest",
                    "fieldName": "title",
                }
            }
        ],
        [
            {
                "extensions": {
                    "code": "undefinedField",
                    "typeName": "PullRequest",
                    "fieldName": "stack",
                }
            },
            {"type": "RATE_LIMITED", "message": "API rate limit exceeded"},
        ],
        [{"type": "FORBIDDEN", "path": ["repository", "pullRequest"], "message": "secret-repo"}],
        [{"type": "NOT_FOUND", "path": ["repository", "pullRequest", "stack"], "message": "x"}],
        [{"type": "FORBIDDEN", "message": "no path"}],
        [{"type": "FORBIDDEN", "path": [*_NODES, 7, "pullRequest"], "message": "no such entry"}],
        [{"type": "SOMETHING_NEW", "path": [*_NODES, 0, "pullRequest"], "message": "x"}],
        "not a list",
        ["not an error"],
        [{"type": "FORBIDDEN", "path": [*_NODES, 0, "pullRequest", 5], "message": "x"}],
        {},
        "",
        0,
        [_undefined_naming(["PullRequest"], "stack")],
        [_undefined_naming("PullRequest", ["stack"])],
        [_undefined_naming({}, "stack")],
    ],
    ids=[
        "message-only",
        "other-undefined-field",
        "undefined-field-and-another",
        "pull-request-forbidden",
        "stack-not-found",
        "no-path",
        "path-to-no-entry",
        "unknown-type",
        "errors-not-a-list",
        "error-not-an-object",
        "hidden-path-part-not-text",
        "errors-empty-object",
        "errors-empty-text",
        "errors-zero",
        "type-name-as-list",
        "field-name-as-list",
        "type-name-as-object",
    ],
)
def test_any_other_error_is_a_failure_that_names_nothing_github_said(
    fake: FakeGraphQL, errors: Any
) -> None:
    fake.answer = _answer([{"position": 1, "pullRequest": None}])
    fake.answer["errors"] = errors
    fake.exit_code = 1

    with pytest.raises(PullRequestError) as failed:
        _read()

    assert str(failed.value) == UNREADABLE


def _without(*path: str) -> dict[str, Any]:
    """The default answer with the key at ``path``, under ``data``, removed."""
    answer = _answer()
    target = answer["data"]
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    return answer


def _member_without(key: str) -> dict[str, Any]:
    member = _member(2)
    del member[key]
    return member


def _with_entry(entry: Any) -> dict[str, Any]:
    """An answer whose stack of three has ``entry`` in the middle."""
    return _answer(
        [
            {"position": 1, "pullRequest": _member(1)},
            entry,
            {"position": 3, "pullRequest": _member(3)},
        ]
    )


def _with(path: list[str], value: Any) -> dict[str, Any]:
    answer = _answer()
    target = answer["data"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return answer


@pytest.mark.parametrize(
    "answer",
    [
        _with(["viewer", "login"], "octo cat"),
        _with(["viewer", "login"], "x" * 65),
        _with(["viewer", "login"], "[bot]"),
        _with(["viewer", "login"], "example-app[bot]x"),
        _with(["repository", "pullRequest", "stackEntry"], {"position": 2**31}),
        _with(["viewer"], None),
        _with(["repository", "pullRequest"], None),
        _with(["repository", "pullRequest", "number"], 3),
        _with(["repository", "pullRequest", "stackEntry"], {"position": "2"}),
        _with(["repository", "pullRequest", "stack", "id"], "PRS_<script>"),
        _with(["repository", "pullRequest", "stack", "id"], "x" * 257),
        _with(["repository", "pullRequest", "stack", "number"], 0),
        _with(["repository", "pullRequest", "stack", "number"], 2**31),
        _with(["repository", "pullRequest", "stack", "size"], -1),
        _with(["repository", "pullRequest", "stack", "size"], 10_001),
        _with(["repository", "pullRequest", "stack", "baseRefName"], "é" * 128),
        _with(["repository", "pullRequest", "stack", "entries", "pageInfo"], None),
        _with(["repository", "pullRequest", "stack", "entries", "nodes"], "none"),
        {"data": None},
        ["not", "an", "answer"],
        # Missing isn't null: an answer without the stack would read as no stack.
        _without("repository", "pullRequest", "stack"),
        _without("repository", "pullRequest", "stackEntry"),
        _without("viewer"),
        _without("viewer", "login"),
        _without("repository", "pullRequest", "number"),
        _without("repository", "pullRequest", "stack", "id"),
        _without("repository", "pullRequest", "stack", "entries", "pageInfo", "hasNextPage"),
        _with(["repository"], None),
        _with(["viewer", "login"], 5),
        _with(["repository", "pullRequest", "number"], "2"),
        _with(["repository", "pullRequest", "stackEntry"], []),
        _with(["repository", "pullRequest", "stackEntry"], {}),
        _with(["repository", "pullRequest", "stack"], []),
        _with(["repository", "pullRequest", "stack", "entries"], None),
        _with(["repository", "pullRequest", "stack", "entries", "nodes"], None),
        _with(["repository", "pullRequest", "stack", "entries", "pageInfo", "hasNextPage"], "no"),
        # A member is an object or null; any other shape isn't GitHub's.
        _with_entry({"position": 2, "pullRequest": "PR 2"}),
        _with_entry({"position": 2, "pullRequest": [_member(2)]}),
        _with_entry({"position": 2, "pullRequest": _member_without("title")}),
        _with_entry({"position": 2, "pullRequest": _member_without("headRepository")}),
        _with_entry({"position": 2, "pullRequest": _member(2, headRepository=REPOSITORY)}),
        _with_entry(
            {
                "position": 2,
                "pullRequest": _member(2, baseRepository={"nameWithOwner": REPOSITORY}),
            }
        ),
        # A malformed entry, dropped quietly, would read as a shorter stack.
        _with_entry("not an entry"),
        _with_entry(None),
        _with_entry({"pullRequest": _member(2)}),
        _with_entry({"position": None, "pullRequest": _member(2)}),
        _with_entry({"position": True, "pullRequest": _member(2)}),
        _with_entry({"position": "2", "pullRequest": _member(2)}),
        _with_entry({"position": 2**31, "pullRequest": _member(2)}),
        _with_entry({"position": -(2**31) - 1, "pullRequest": _member(2)}),
        _with_entry({"position": 2}),
    ],
    ids=[
        "viewer-with-space",
        "viewer-too-long",
        "viewer-only-suffix",
        "viewer-text-after-suffix",
        "position-past-int",
        "no-viewer",
        "no-pull-request",
        "other-pull-request",
        "position-as-text",
        "stack-id-form",
        "stack-id-too-long",
        "stack-number-zero",
        "stack-number-past-int",
        "stack-size-negative",
        "stack-size-too-large",
        "stack-base-256-bytes",
        "no-page-info",
        "nodes-not-a-list",
        "no-data",
        "list",
        "no-stack-key",
        "no-stack-entry-key",
        "no-viewer-key",
        "no-login-key",
        "no-pull-request-number-key",
        "no-stack-id-key",
        "no-has-next-page-key",
        "no-repository",
        "viewer-login-number",
        "pull-request-number-as-text",
        "stack-entry-as-list",
        "stack-entry-no-position",
        "stack-as-list",
        "entries-null",
        "nodes-null",
        "has-next-page-as-text",
        "member-as-text",
        "member-as-list",
        "member-no-title-key",
        "member-no-head-repository-key",
        "head-repository-as-text",
        "base-repository-no-default-branch-key",
        "entry-not-an-object",
        "entry-null",
        "entry-no-position",
        "entry-position-null",
        "entry-position-boolean",
        "entry-position-as-text",
        "entry-position-past-int",
        "entry-position-below-int",
        "entry-no-pull-request-key",
    ],
)
def test_an_answer_that_isnt_githubs_is_a_failure(fake: FakeGraphQL, answer: Any) -> None:
    fake.answer = answer

    with pytest.raises(PullRequestError) as failed:
        _read()

    assert str(failed.value) == UNREADABLE


@pytest.mark.parametrize("output", ["", "<html>example-org/secret-repo</html>"])
def test_an_answer_gh_couldnt_give_names_nothing_it_said(fake: FakeGraphQL, output: str) -> None:
    fake.raw_output = output
    fake.exit_code = 1

    with pytest.raises(PullRequestError) as failed:
        _read()

    assert str(failed.value) == UNREADABLE


def test_a_pr_on_a_host_never_set_up_runs_no_gh_with_an_enterprise_token(
    fake: FakeGraphQL, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh would treat every enterprise host as signed in and send it this token."""
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", TOKEN)

    with pytest.raises(PullRequestError, match="Sign in to this GitHub host with gh"):
        _read(f"https://{GHES}/{REPOSITORY}/pull/2")

    assert fake.calls == []


def test_a_pr_on_the_gh_host_set_up_is_read_with_no_sign_in_probe(
    fake: FakeGraphQL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_HOST", GHES)
    fake.answer = _answer([])

    _read(f"https://{GHES}/{REPOSITORY}/pull/2")

    (call,) = fake.calls
    assert call[call.index("--hostname") + 1] == GHES


def test_the_read_runs_as_the_account_saved_for_the_repository(
    fake: FakeGraphQL, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = {f"github.com/{REPOSITORY}": "octocat"}
    monkeypatch.setattr(github._config, "github_account_preference", saved.get)

    _read()

    assert fake.calls[0] == ["auth", "token", "--user", "octocat", "-h", "github.com"]
    assert fake.envs[1] is not None and fake.envs[1]["GH_TOKEN"] == TOKEN


def test_with_no_saved_account_the_read_runs_as_ghs_active_one(fake: FakeGraphQL) -> None:
    _read()

    assert [call[0] for call in fake.calls] == ["api"]
    assert fake.envs == [None]
