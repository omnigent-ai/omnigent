"""Track a GraphQL-created PR by its returned identity, including native hooks."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from omnigent.runner.pr_observer import extract_prs, observe_hook
from omnigent.runner.session_prs import SessionPrRegistry

URL = "https://github.com/example/project/pull/42"
PR_PATH = ".data.createPullRequest.pullRequest"
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


def creation_output(projection: str | None, url: str | None = URL) -> str:
    pr = {"url": url, "number": int(url.rsplit("/", 1)[1])} if url else None
    if projection and projection.endswith(".url"):
        return url if url else "null"
    return json.dumps(
        pr if projection else {"data": {"createPullRequest": {"pullRequest": pr}}}, indent=2
    )


@pytest.mark.parametrize("wrapped", [False, True], ids=["shell", "login-shell"])
@pytest.mark.parametrize("commit_first", [False, True], ids=["create", "commit-push-create"])
@pytest.mark.parametrize(
    "projection",
    [None, ".data.createPullRequest.pullRequest", ".data.createPullRequest.pullRequest.url"],
)
def test_graphql_create_tracks_returned_pr(
    wrapped: bool, commit_first: bool, projection: str | None
) -> None:
    shell = command(projection=projection)
    if commit_first:
        shell = "git commit -m 'A change' && git push && " + shell
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
    if commit_first:
        output = "[contributor/topic 1a2b3c4] A change\n" + output
    references, created = extract_prs(
        "exec_command", {"cmd": shell}, {"exit_code": 0, "output": output}
    )
    assert [ref.url for ref in references] == [URL]
    assert created


@pytest.mark.parametrize("transport", ["runner-shell", "synthetic-success-footer"])
@pytest.mark.parametrize(
    "projection",
    [None, ".data.createPullRequest.pullRequest", ".data.createPullRequest.pullRequest.url"],
)
def test_graphql_shell_result_formats(transport: str, projection: str | None) -> None:
    """Exercise serialized runner output and tolerance for a synthetic success footer."""
    output = "[1 (root-commit) 1a2b3c4] A change\n" + creation_output(projection)
    if transport == "runner-shell":
        output = json.dumps({"stdout": output, "stderr": "", "exit_code": 0})
    else:
        output += "\n[exit code: 0]"
    references, created = extract_prs(
        "sys_os_shell" if transport == "runner-shell" else "shell",
        {"command": "git commit -m 'A change' && " + command(projection=projection)},
        output,
    )
    assert [ref.url for ref in references] == [URL]
    assert created


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "projection",
    [None, ".data.createPullRequest.pullRequest", ".data.createPullRequest.pullRequest.url"],
)
def test_graphql_and_cli_creations_preserve_both_identities(
    reverse: bool, projection: str | None
) -> None:
    other = "https://github.com/example/another/pull/7"
    commands = [
        command(projection=projection),
        "gh pr create -R example/another",
    ]
    outputs = [creation_output(projection), other]
    references, created = extract_prs(
        "shell",
        {"command": "; ".join(reversed(commands) if reverse else commands)},
        "\n".join(reversed(outputs) if reverse else outputs),
    )
    assert {ref.url for ref in references} == {URL, other}
    assert created


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "projection",
    [None, ".data.createPullRequest.pullRequest", ".data.createPullRequest.pullRequest.url"],
)
def test_graphql_creation_with_targeted_edit_keeps_both_prs(
    reverse: bool, projection: str | None
) -> None:
    other = "https://github.com/example/other/pull/7"
    commands = [command(projection=projection), "gh pr edit 7 -R example/other --add-label ready"]
    outputs = [creation_output(projection), other]
    references, created = extract_prs(
        "shell",
        {"command": "; ".join(reversed(commands) if reverse else commands)},
        "\n".join(reversed(outputs) if reverse else outputs),
    )
    assert {ref.url for ref in references} == {URL, other}
    assert not created


@pytest.mark.parametrize(
    "body_argument",
    ["--body '>'", '--body "<"', r"--body \>", "--body='>'", r"--body=\>", "--body '>>'"],
)
def test_quoted_redirect_arguments_do_not_suppress_creation(body_argument: str) -> None:
    other = "https://github.com/example/other/pull/7"
    references, created = extract_prs(
        "shell",
        {"command": command() + f"; gh pr edit 7 -R example/other {body_argument}"},
        {"exit_code": 0, "stdout": creation_output(None) + "\n" + other},
    )
    assert {ref.url for ref in references} == {URL, other}
    assert not created


@pytest.mark.parametrize(
    "shell_pattern,projection",
    [
        ("{create}; cat saved-pr-output.txt", None),
        ("cat saved-pr-output.txt; {create}", None),
        ("{create} >/dev/null", None),
        ("{create} 1>>saved-pr-output.txt", None),
        ("{wrapped} >/dev/null", None),
        ("{create} --silent", None),
        ("{create} --silent=true", None),
        ("{create} | gh pr create", None),
        ("{create} & wait", None),
        ("git log -1 --format=%B; {create}", None),
        ("sh -c 'false || cat saved-pr-output.txt'; {create}", None),
        ("{create}; cat saved-pr-output.txt", PR_PATH),
        ("{create}; cat saved-pr-output.txt", f"{PR_PATH}.url"),
    ],
    ids=[
        "later-output",
        "earlier-output",
        "redirect",
        "append",
        "outer-redirect",
        "silent",
        "silent-value",
        "pipe",
        "background",
        "git-content",
        "nested-fallback-output",
        "object-projection",
        "url-projection",
    ],
)
def test_graphql_unresolved_stdout_cannot_replace_absent_creation(
    shell_pattern: str, projection: str | None
) -> None:
    create = command(projection=projection)
    shell = shell_pattern.format(create=create, wrapped=shlex.join(["sh", "-c", create]))
    references, _ = extract_prs(
        "shell",
        {"command": shell},
        {
            "exit_code": 0,
            "stdout": creation_output(projection, "https://github.com/example/unrelated/pull/9"),
        },
    )
    assert not references


def test_graphql_unresolved_stdout_preserves_explicit_targets() -> None:
    references, created = extract_prs(
        "shell",
        {
            "command": command()
            + " >/dev/null; cat saved-pr-output.txt; gh pr edit 7 -R example/other -t updated"
        },
        {"exit_code": 0, "stdout": creation_output(None)},
    )
    assert [ref.url for ref in references] == ["https://github.com/example/other/pull/7"]
    assert not created


def test_graphql_assignment_prefix_does_not_hide_an_output_command() -> None:
    unrelated = "https://github.com/example/unrelated/pull/9"
    edited = "https://github.com/example/other/pull/7"
    references, _ = extract_prs(
        "shell",
        {
            "command": "repo_id=$(printf '(') "
            + shlex.join(["printf", "%s\n", unrelated, ")"])
            + "; "
            + command(projection=".data.createPullRequest.pullRequest.url")
            + "; gh pr edit 7 -R example/other -t updated"
        },
        {"exit_code": 0, "stdout": unrelated + "\n)\n" + edited},
    )
    assert [ref.url for ref in references] == [edited]


@pytest.mark.parametrize("hook_pr_url", [False, True], ids=["no-hook-url", "hook-url"])
@pytest.mark.parametrize(
    "projection",
    [None, ".data.createPullRequest.pullRequest", ".data.createPullRequest.pullRequest.url"],
)
def test_graphql_checkout_preparation_checks_output_ambiguity(
    projection: str | None, hook_pr_url: bool
) -> None:
    prefix = "https://github.com/example/other/pull/7\n" if hook_pr_url else ""
    references, created = extract_prs(
        "shell",
        {
            "command": "set -euo pipefail; cd /repo; git -C /repo add . && "
            "git -C /repo commit -m 'A change' && git -C /repo push && "
            + command(projection=projection)
        },
        {"exit_code": 0, "stdout": prefix + creation_output(projection)},
    )
    ambiguous = hook_pr_url and projection == ".data.createPullRequest.pullRequest.url"
    assert [ref.url for ref in references] == ([] if ambiguous else [URL])
    assert created


@pytest.mark.parametrize(
    "projection",
    [None, ".data.createPullRequest.pullRequest", ".data.createPullRequest.pullRequest.url"],
)
def test_multiple_graphql_creations_preserve_returned_identities(
    projection: str | None,
) -> None:
    other = "https://github.com/example/project/pull/43"
    commands = [
        command(projection=projection),
        command(QUERY.replace('"contributor/topic"', '"contributor/another"'), projection),
    ]
    references, created = extract_prs(
        "shell",
        {"command": "; ".join(commands)},
        "\n".join(creation_output(projection, url) for url in (URL, other)),
    )
    assert {ref.url for ref in references} == {URL, other}
    assert created


def test_multiple_graphql_response_aliases_preserve_returned_identities() -> None:
    other = "https://github.com/example/project/pull/43"
    aliased_query = QUERY.replace("createPullRequest(", "opened: createPullRequest(")
    references, created = extract_prs(
        "shell",
        {"command": command() + "; " + command(aliased_query)},
        creation_output(None)
        + "\n"
        + creation_output(None, other).replace('"createPullRequest"', '"opened"'),
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


@pytest.mark.parametrize("compound", [False, True], ids=["single", "shared-output"])
@pytest.mark.parametrize(
    "formatters",
    [
        ("--jq", f"{PR_PATH}.body"),
        ("--template", "{{" + PR_PATH + ".body}}"),
        ("-t", "{{" + PR_PATH + ".body}}"),
        ("--jq", f"{PR_PATH}.url", "--jq", f"{PR_PATH}.body"),
        ("--jq", f"{PR_PATH}.url", "-q", f"{PR_PATH}.body"),
        ("-q", f"{PR_PATH}.url", "--jq", f"{PR_PATH}.body"),
        (f"-q{PR_PATH}.url", f"-q{PR_PATH}.body"),
        (f"--jq={PR_PATH}.url", f"--jq={PR_PATH}.body"),
    ],
    ids=[
        "jq",
        "template",
        "short-template",
        "long-long",
        "long-short",
        "short-long",
        "attached",
        "equals",
    ],
)
def test_graphql_body_projection_is_not_pr_identity(
    formatters: tuple[str, ...], compound: bool
) -> None:
    query = QUERY.replace("number url title isDraft", "number url title isDraft body")
    shell = command(query) + " " + shlex.join(formatters)
    if compound:
        shell += "; gh pr create --repo example/another"
    references, _ = extract_prs(
        "shell", {"command": shell}, "https://github.com/example/mentioned/pull/99"
    )
    assert not references


@pytest.mark.parametrize(
    "previous,formatters,projection",
    [
        (f"{PR_PATH}.body", ("--jq", f"{PR_PATH}.url"), f"{PR_PATH}.url"),
        (f"{PR_PATH}.url", ("-q", PR_PATH), PR_PATH),
        (PR_PATH, (f"--jq={PR_PATH}.url",), f"{PR_PATH}.url"),
    ],
    ids=["body-to-url", "url-to-object", "object-to-url"],
)
def test_graphql_last_formatter_selects_output_shape(
    previous: str, formatters: tuple[str, ...], projection: str
) -> None:
    query = QUERY.replace("number url title isDraft", "number url title isDraft body")
    references, created = extract_prs(
        "shell",
        {"command": command(query, previous) + " " + shlex.join(formatters)},
        {"exit_code": 0, "stdout": creation_output(projection)},
    )
    assert [ref.url for ref in references] == [URL]
    assert created


def test_graphql_nested_aliases_do_not_supply_pr_identity() -> None:
    query = QUERY.replace("number url title isDraft", "url: body")
    body_url = "https://github.com/example/mentioned/pull/99"
    references, created = extract_prs(
        "shell",
        {"command": command(query, projection=".data.createPullRequest.pullRequest.url")},
        body_url,
    )
    assert not references
    assert not created


def test_graphql_alias_uses_its_response_identity() -> None:
    query = QUERY.replace("  createPullRequest(", "  opened: createPullRequest(")
    result = {"data": {"opened": {"pullRequest": {"url": URL}}}}
    references, created = extract_prs("shell", {"command": command(query)}, result)
    assert [ref.url for ref in references] == [URL]
    assert created


@pytest.mark.parametrize(
    "operation,body",
    [
        (
            "createPullRequest",
            json.dumps('A "quoted" path: C:\\work\\repo.\ncreatePullRequest(input: {})'),
        ),
        (
            "createPullRequest",
            '"""A note about C:\\work\\repo.\nExample: createPullRequest(input: {})\n"""',
        ),
        ("createPullRequest", r'"""Code sample: \"""createPullRequest\""". Keep this text."""'),
        ("createPullRequest", r'"""An escaped delimiter and quote: \"""" remain text."""'),
        (
            "createPullRequest",
            json.dumps("An ordinary paragraph in the pull request description.\n" * 400),
        ),
        (
            "addComment",
            json.dumps('A "quoted" path: C:\\work\\repo.\ncreatePullRequest(input: {})'),
        ),
    ],
    ids=[
        "quoted-string",
        "multiline-block",
        "escaped-block-quotes",
        "escaped-block-delimiter-and-quote",
        "long-description",
        "other-operation",
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


@pytest.mark.parametrize(
    "prefix,projection",
    [
        ('{"message":\n', None),
        ("[\n", None),
        ('"unfinished\n', None),
        ("[INFO] building\n", None),
        ('{"message":\n', PR_PATH),
        ('{"message":\n', f"{PR_PATH}.url"),
    ],
)
def test_incomplete_json_output_is_not_reparsed_as_pr_identity(
    prefix: str, projection: str | None
) -> None:
    references, _ = extract_prs(
        "shell",
        {"command": command(projection=projection)},
        prefix + creation_output(projection),
    )
    assert not references


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("response_shaped", [False, True])
def test_full_response_reads_only_the_mutation_identity(
    created: bool, reverse: bool, response_shaped: bool
) -> None:
    other = "https://github.com/example/another/pull/7"
    unrelated = creation_output(None, other) if response_shaped else json.dumps({"url": other})
    outputs = [creation_output(None, URL if created else None), unrelated]
    references, _ = extract_prs(
        "shell",
        {"command": command()},
        {"stdout": "\n".join(reversed(outputs) if reverse else outputs)},
    )
    assert [ref.url for ref in references] == ([URL] if created and not response_shaped else [])


@pytest.mark.parametrize(
    "projection,wrapper",
    [
        (PR_PATH, "stdout"),
        (PR_PATH, "serialized-stdout"),
        (PR_PATH, "aggregatedOutput"),
        (PR_PATH, "structuredContent"),
        (PR_PATH, "content"),
        (None, "stdout"),
        (f"{PR_PATH}.url", "stdout"),
    ],
)
def test_graphql_output_is_separate_from_tool_metadata(
    projection: str | None, wrapper: str
) -> None:
    unrelated = {"url": "https://github.com/example/another/pull/7"}
    output = creation_output(projection)
    value = [{"type": "text", "text": output}] if wrapper == "content" else output
    result: object = {
        "url": unrelated["url"],
        "metadata": unrelated,
        "stdout" if wrapper == "serialized-stdout" else wrapper: value,
    }
    if wrapper != "content":
        result["content"] = unrelated
    if wrapper == "serialized-stdout":
        result = json.dumps({**result, "exit_code": 0})
    references, created = extract_prs(
        "sys_os_shell" if wrapper == "serialized-stdout" else "shell",
        {"command": command(projection=projection)},
        result,
    )
    assert [ref.url for ref in references] == [URL]
    assert created


@pytest.mark.parametrize(
    "projection",
    [None, ".data.createPullRequest.pullRequest", ".data.createPullRequest.pullRequest.url"],
)
def test_empty_graphql_output_does_not_read_other_tool_fields(projection: str | None) -> None:
    unrelated = {"url": URL}
    result = {
        "stdout": creation_output(projection, None),
        "content": unrelated,
        "structuredContent": unrelated,
        "metadata": unrelated,
        "stderr": json.dumps(unrelated),
    }
    references, _ = extract_prs("shell", {"command": command(projection=projection)}, result)
    assert not references


@pytest.mark.parametrize(
    "projection",
    [None, ".data.createPullRequest.pullRequest", ".data.createPullRequest.pullRequest.url"],
)
@pytest.mark.parametrize("tool", ["sys_os_shell", "exec_command"])
def test_graphql_emitted_documents_are_not_tool_envelopes(
    projection: str | None, tool: str
) -> None:
    document = json.dumps({"stdout": creation_output(projection), "exit_code": 0})
    result = (
        json.dumps({"stdout": document, "stderr": "", "exit_code": 0})
        if tool == "sys_os_shell"
        else document
    )
    references, _ = extract_prs(tool, {"command": command(projection=projection)}, result)
    assert not references


@pytest.mark.parametrize("field", ["content", "metadata", "stderr"])
def test_graphql_projection_does_not_read_metadata_without_output(field: str) -> None:
    result = {field: {"url": URL}}
    references, _ = extract_prs(
        "shell", {"command": command(projection=".data.createPullRequest.pullRequest")}, result
    )
    assert not references


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "projection",
    [".data.createPullRequest.pullRequest", ".data.createPullRequest.pullRequest.url"],
)
def test_ambiguous_projected_results_are_not_attributed(
    created: bool, reverse: bool, projection: str
) -> None:
    unrelated = creation_output(projection, "https://github.com/example/another/pull/7")
    outputs = [creation_output(projection, URL if created else None), unrelated]
    references, _ = extract_prs(
        "shell",
        {"command": command(projection=projection)},
        "\n".join(reversed(outputs) if reverse else outputs),
    )
    assert not references


@pytest.mark.timeout(5)
def test_unterminated_block_string_fails_fast() -> None:
    # A backslash before every character is the worst case for string scanning.
    query = '"""' + "\\a" * 2000
    assert extract_prs("shell", {"command": command(query)}, URL) == ([], False)
