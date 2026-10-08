"""Track a GraphQL-created PR by its returned identity, including native hooks."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from omnigent.runner.pr_observer import extract_prs, observe_hook
from omnigent.runner.session_prs import SessionPrRegistry

URL = "https://github.com/example/project/pull/42"
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


@pytest.mark.parametrize("reverse", [False, True])
def test_graphql_and_cli_creations_preserve_both_identities(reverse: bool) -> None:
    other = "https://github.com/example/another/pull/7"
    commands = [
        command(projection=".data.createPullRequest.pullRequest.url"),
        "gh pr create -R example/another",
    ]
    references, created = extract_prs(
        "shell",
        {"command": "; ".join(reversed(commands) if reverse else commands)},
        URL + "\n" + other,
    )
    assert {ref.url for ref in references} == {URL, other}
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


@pytest.mark.parametrize("compound", [False, True], ids=["single", "shared-output"])
@pytest.mark.parametrize("formatter", ["--jq", "--template", "-t"])
def test_graphql_body_projection_is_not_pr_identity(formatter: str, compound: bool) -> None:
    query = QUERY.replace("number url title isDraft", "number url title isDraft body")
    path = ".data.createPullRequest.pullRequest.body"
    projection = path if formatter == "--jq" else "{{" + path + "}}"
    shell = command(query) + " " + shlex.join([formatter, projection])
    if compound:
        shell += "; gh pr create --repo example/another"
    references, _ = extract_prs(
        "shell", {"command": shell}, "https://github.com/example/mentioned/pull/99"
    )
    assert not references


@pytest.mark.parametrize(
    "projection",
    [None, ".data.createPullRequest.pullRequest", ".data.createPullRequest.pullRequest.url"],
)
def test_graphql_nested_aliases_do_not_supply_pr_identity(projection: str | None) -> None:
    query = QUERY.replace("number url title isDraft", "url: body")
    body_url = "https://github.com/example/mentioned/pull/99"
    pr = {"url": body_url}
    output = (
        body_url
        if projection and projection.endswith(".url")
        else json.dumps(pr if projection else {"data": {"createPullRequest": {"pullRequest": pr}}})
    )
    references, created = extract_prs(
        "shell", {"command": command(query, projection=projection)}, output
    )
    assert not references
    assert not created


def test_graphql_alias_uses_its_response_identity() -> None:
    query = QUERY.replace("  createPullRequest(", "  opened: createPullRequest(")
    result = {"data": {"opened": {"pullRequest": {"url": URL}}}}
    references, created = extract_prs("shell", {"command": command(query)}, result)
    assert [ref.url for ref in references] == [URL]
    assert created


@pytest.mark.parametrize("operation", ["createPullRequest", "addComment"])
@pytest.mark.parametrize(
    "body",
    [
        json.dumps('A "quoted" path: C:\\work\\repo.\ncreatePullRequest(input: {})'),
        '"""A note about C:\\work\\repo.\nExample: createPullRequest(input: {})\n"""',
        r'"""Code sample: \"""createPullRequest\""". Keep this text."""',
        r'"""An escaped delimiter and quote: \"""" remain text."""',
        json.dumps("An ordinary paragraph in the pull request description.\n" * 400),
    ],
    ids=[
        "quoted-string",
        "multiline-block",
        "escaped-block-quotes",
        "escaped-block-delimiter-and-quote",
        "long-description",
    ],
)
def test_graphql_string_contents_do_not_change_operation(operation: str, body: str) -> None:
    query = QUERY.replace('title: "A change"', f'title: "A change", body: {body}').replace(
        "createPullRequest(input:", f"{operation}(input:", 1
    )
    references, created = extract_prs(
        "shell",
        {"command": command(query, projection=".data.createPullRequest.pullRequest")},
        {"url": URL, "number": 42},
    )
    expected = operation == "createPullRequest"
    assert [ref.url for ref in references] == ([URL] if expected else [])
    assert created is expected


@pytest.mark.parametrize("literal", ['"unfinished', '"""unfinished'])
def test_unfinished_graphql_string_does_not_attach_pr(literal: str) -> None:
    query = (
        "mutation { createPullRequest(input: {title: " + literal + "}) { pullRequest { url } } }"
    )
    assert extract_prs("shell", {"command": command(query)}, URL) == ([], False)


def test_graphql_errors_do_not_supply_pr_identity() -> None:
    references, _ = extract_prs(
        "shell",
        {"command": command()},
        {"data": {"createPullRequest": None}, "errors": [{"message": URL}]},
    )
    assert not references


@pytest.mark.parametrize("projection", [None, ".data.createPullRequest.pullRequest"])
def test_graphql_json_output_does_not_fall_back_to_unrelated_url(
    projection: str | None,
) -> None:
    output = json.dumps({"data": {"createPullRequest": None}}) + "\n" + URL
    references, _ = extract_prs("shell", {"command": command(projection=projection)}, output)
    assert not references


@pytest.mark.timeout(5)
def test_unterminated_block_string_fails_fast() -> None:
    # A backslash before every character is the worst case for string scanning.
    query = '"""' + "\\a" * 2000
    assert extract_prs("shell", {"command": command(query)}, URL) == ([], False)
