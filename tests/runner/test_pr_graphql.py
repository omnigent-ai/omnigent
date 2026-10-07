"""Track a GraphQL-created PR by its returned identity, including native hooks."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from omnigent.runner.pr_observer import extract_prs, observe_hook
from omnigent.runner.session_prs import SessionPrRegistry

URL = "https://github.com/example/project/pull/42"
OTHER_URL = "https://github.com/other/repo/pull/99"
QUERY = """mutation CreatePullRequest($repositoryId: ID!, $headRepositoryId: ID!) {
  createPullRequest(input: {
    repositoryId: $repositoryId, headRepositoryId: $headRepositoryId,
    baseRefName: "main", headRefName: "contributor/topic", title: "A change"
  }) { pullRequest { number url title isDraft } }
}"""


def command(query: str = QUERY, projection: str | None = None) -> str:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    if projection:
        args.extend(["--jq", projection])
    return shlex.join(args)


@pytest.mark.parametrize("wrapped", [False, True], ids=["shell", "login-shell"])
@pytest.mark.parametrize(
    "projection",
    [None, ".data.createPullRequest.pullRequest", ".data.createPullRequest.pullRequest.url"],
)
def test_graphql_create_tracks_returned_pr(wrapped: bool, projection: str | None) -> None:
    shell = command(projection=projection)
    if wrapped:
        shell = shlex.join(
            ["/bin/zsh", "-lc", "repo_id=$(gh api repos/example/project --jq .node_id)\n" + shell]
        )
    pr = {"number": 42, "url": URL, "title": "A change", "isDraft": True}
    output = (
        URL
        if projection and projection.endswith(".url")
        else json.dumps(pr if projection else {"data": {"createPullRequest": {"pullRequest": pr}}})
    )
    references, created = extract_prs(
        "exec_command", {"cmd": shell}, {"exit_code": 0, "output": output}
    )
    assert [ref.url for ref in references] == [URL]
    assert created


def test_native_graphql_creation_persists_explicit_repo_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_use_id": "create-pr",
        "tool_name": "shell",
        "tool_input": {
            "command": command(projection=".data.createPullRequest.pullRequest"),
            "cwd": "/old-checkout",
        },
        "tool_response": {"exit_code": 0, "stdout": json.dumps({"number": 42, "url": URL})},
    }
    observe_hook("session", payload)
    observe_hook("session", payload)
    entries = SessionPrRegistry("session").list()
    assert [(entry.url, entry.relationship) for entry in entries] == [(URL, "created")]


@pytest.mark.parametrize(
    "query",
    [
        'query { repository(owner: "example", name: "project") '
        "{ pullRequest(number: 42) { url } } }",
        'mutation { addComment(input: {body: "createPullRequest(input: {})", '
        'subjectId: "PR"}) { subject { url } } }',
        "# mutation { createPullRequest(input: {}) { pullRequest { url } } }\n"
        "query { viewer { url } }",
        'mutation { addComment(input: {body: """createPullRequest(input: {})""", '
        'subjectId: "PR"}) { subject { url } } }',
        'mutation { createPullRequest: addComment(input: {body: "text", '
        'subjectId: "PR"}) { subject { url } } }',
        QUERY + "\nquery AnotherOperation { viewer { url } }",
        QUERY[:-1] + ' addComment(input: {subjectId: "PR", body: "text"}) { subject { url } } }',
        QUERY.replace("createPullRequest(input:", "addComment(input:"),
        "$query",
        "@query.graphql",
    ],
)
def test_other_graphql_operations_do_not_attach_prs(query: str) -> None:
    assert extract_prs("shell", {"command": command(query)}, URL) == ([], False)


@pytest.mark.parametrize("exit_code", [1, None])
def test_unsuccessful_graphql_create_does_not_attach_pr(exit_code: int | None) -> None:
    result = {"exit_code": exit_code, "stdout": URL}
    if exit_code is None:
        result["session_id"] = "still-running"
    references, _ = extract_prs("exec_command", {"cmd": command()}, result)
    assert not references


def test_graphql_body_projection_is_not_pr_identity() -> None:
    references, _ = extract_prs(
        "shell", {"command": command(projection=".data.createPullRequest.pullRequest.body")}, URL
    )
    assert not references


def test_graphql_alias_uses_its_response_identity() -> None:
    query = QUERY.replace("  createPullRequest(", "  opened: createPullRequest(")
    result = {"data": {"opened": {"pullRequest": {"url": URL}}}}
    references, created = extract_prs("shell", {"command": command(query)}, result)
    assert [ref.url for ref in references] == [URL]
    assert created


def test_graphql_errors_do_not_supply_pr_identity() -> None:
    references, _ = extract_prs(
        "shell",
        {"command": command()},
        {"data": {"createPullRequest": None}, "errors": [{"message": URL}]},
    )
    assert not references


def test_unterminated_block_string_fails_fast() -> None:
    # A backslash before every character is the worst case for string scanning.
    query = '"""' + "\\a" * 2000
    assert extract_prs("shell", {"command": command(query)}, URL) == ([], False)


@pytest.mark.parametrize(
    "projection,result",
    [
        (None, {"data": {"createPullRequest": {"pullRequest": {"url": OTHER_URL}}}}),
        (".data.createPullRequest.pullRequest", {"url": OTHER_URL}),
    ],
)
def test_nested_alias_cannot_relabel_body_as_identity(
    projection: str | None, result: dict[str, object]
) -> None:
    query = QUERY.replace("pullRequest { number url title", "pullRequest { number url: body title")
    assert "url: body" in query
    assert extract_prs("shell", {"command": command(query, projection)}, result) == ([], False)


def test_template_output_is_not_pr_identity() -> None:
    shell = shlex.join(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={QUERY}",
            "--template",
            "{{.data.createPullRequest.pullRequest.body}}",
        ]
    )
    references, _ = extract_prs("shell", {"command": shell}, OTHER_URL)
    assert not references
