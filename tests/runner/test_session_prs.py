"""Session PR identity, evidence extraction, and durable concurrent updates."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omnigent.runner.pr_observer import extract_prs, observe_hook
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry

A = "https://github.com/example/one/pull/42"
B = "https://github.com/example/two/pull/42"

_MCP_CREATE_SUMMARY = (
    "=== PULL REQUEST CREATED ===\n\n"
    "✓ Successfully created PR #42\n\n"
    f"View PR: {A}\n"
    "Labels: [ai-assisted]\n\n"
    "Next steps:\n  • Assign reviewers\n  • Add labels (if needed)\n  • Monitor CI checks\n"
)

_REST_CREATE_COMMAND = (
    "cd ~/workspace && "
    "HEAD_SHA=$(git rev-parse contributor/docs-test) && "
    'echo "HEAD SHA: $HEAD_SHA" && '
    r"""gh api /repos/example/project-dev/pulls \
  --method POST \
  --field title="docs(readme): clarify repo is a monorepo" \
  --field head="contributor/docs-test" \
  --field base="master" \
  --field body="## Summary
- Adds \"Monorepo\" to the README title as a test PR.

## Test Plan
- N/A — single-word documentation change.

## Demo
N/A

## Type of change
- [x] Documentation / non-breaking change

## Test coverage
- [x] Not applicable

## Coverage notes
Single-word documentation change only.

This pull request and its description were written by an agent." \
  --jq '.html_url' 2>&1"""
)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/one/issues/42",
        "file:///example/one/pull/42",
        "https://token@github.com/example/one/pull/42",
        "https://localhost/example/one/pull/42",
        "https://github.com/../one/pull/42",
        "https://github.com/example/one/pull/0",
    ],
)
def test_invalid_reference(url: str) -> None:
    with pytest.raises(ValueError):
        PullRequestRef.from_url(url)


def test_reference_normalizes_identity() -> None:
    assert (
        PullRequestRef.from_url(
            A.replace("github.com/example/one", "GITHUB.COM/EXAMPLE/ONE") + "/files#diff"
        ).url
        == A
    )


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "git..example.com",
        "-git.example.com",
        "git-.example.com",
        "git_host.example.com",
        "github.com.",
        "gíthub.com",
        pytest.param("a" * 64 + ".example.com", id="overlong-label"),
        pytest.param(".".join(["a" * 63] * 4), id="overlong-host"),
        pytest.param("0" * 100_000, id="large-numeric-host"),
    ],
)
def test_reference_rejects_invalid_hostname(host: str) -> None:
    with pytest.raises(ValueError):
        PullRequestRef.from_url(f"https://{host}/example/one/pull/42")


@pytest.mark.parametrize(
    "host",
    [
        "github.com",
        "git-2.example.internal",
        pytest.param(".".join(["a" * 63] * 3 + ["a" * 61]), id="maximum-dns-length"),
    ],
)
def test_reference_accepts_dns_hostname(host: str) -> None:
    url = f"https://{host}/example/one/pull/42"
    assert PullRequestRef.from_url(url).url == url


@pytest.mark.parametrize(
    "name,args,result,urls",
    [
        ("Bash", {"command": "gh pr create --title test"}, {"stdout": A + "\n"}, [A]),
        ("shell", {"command": "env FOO=bar /usr/bin/gh pr create"}, A, [A]),
        ("exec_command", {"cmd": "bash -lc 'cd /repo && gh pr create'"}, {"output": A}, [A]),
        ("Bash", {"command": "gh pr create; gh pr create -R example/two"}, A + "\n" + B, [A, B]),
        ("Bash", {"command": "gh pr create"}, {"stdout": A, "exit_code": 1}, []),
        ("Bash", {"command": "gh pr create"}, A + "\n[exit code: 1]", []),
        ("Bash", {"command": f"echo '{A}'"}, A, []),
        ("Bash", {"command": "gh pr list"}, A + "\n" + B, []),
        ("Bash", {"command": "gh pr view 42"}, A, []),
        (
            "mcp__custom__create_pull_request",
            {"owner": "example", "repo": "one"},
            {"content": [{"type": "text", "text": json.dumps({"url": A, "body": B})}]},
            [A],
        ),
        (
            "mcp__plugin_my-plugin_gh__create_pull_request",
            {},
            {"structuredContent": {"html_url": B}},
            [B],
        ),
        ("mcp__gh__create_pull_request", {}, {"isError": True, "content": [{"text": A}]}, []),
        ("mcp__gh__list_pull_requests", {}, {"data": [{"url": A}]}, []),
        (
            "mcp__gh__update_pull_request",
            {"owner": "example", "repo": "one", "pullNumber": 42},
            {},
            [A],
        ),
        ("Bash", {"command": "gh pr edit 42 -R example/two --title test"}, "Updated", [B]),
        (
            "Bash",
            {"command": "gh api repos/example/one/pulls -X POST"},
            {"stdout": json.dumps({"html_url": A})},
            [A],
        ),
    ],
)
def test_extract_completed_operations(
    name: str, args: dict, result: object, urls: list[str]
) -> None:
    references, _ = extract_prs(name, args, result)
    assert [ref.url for ref in references] == urls


def test_independent_repos_and_restart(tmp_path: Path) -> None:
    store = SessionPrRegistry("conv_a", root=tmp_path)
    store.record(
        [PullRequestRef.from_url(A), PullRequestRef.from_url(B)],
        relationship="created",
        source="test",
        observation_id="call1",
        timestamp=10,
    )
    restored = SessionPrRegistry("conv_a", root=tmp_path)
    assert {entry.url for entry in restored.list()} == {A, B}
    restored.record(
        [PullRequestRef.from_url(A)],
        relationship="worked_on",
        source="replay",
        observation_id="call1",
        timestamp=99,
    )
    assert all(entry.last_seen_at == 10 for entry in restored.list())
    assert SessionPrRegistry("conv_b", root=tmp_path).list() == []
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_removal_survives_replay_and_inference(tmp_path: Path) -> None:
    store = SessionPrRegistry("conv_a", root=tmp_path)
    reference = PullRequestRef.from_url(A)
    store.record([reference], relationship="created", source="test")
    store.remove(A)
    store.record([reference], relationship="inferred", source="branch")
    assert store.list() == []
    store.record([reference], relationship="attached", source="user")
    assert [entry.url for entry in store.list()] == [A]


def test_concurrent_writers_preserve_all_prs(tmp_path: Path) -> None:
    def write(number: int) -> None:
        store = SessionPrRegistry("conv_a", root=tmp_path)
        store.record(
            [PullRequestRef.from_url(f"https://github.com/example/one/pull/{number}")],
            relationship="created",
            source="test",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(1, 17)))
    assert len(SessionPrRegistry("conv_a", root=tmp_path).list()) == 16


def test_corruption_is_not_overwritten(tmp_path: Path) -> None:
    store = SessionPrRegistry("conv_a", root=tmp_path)
    store.path.write_text("broken")
    with pytest.raises(ValueError):
        store.record([PullRequestRef.from_url(A)], relationship="created", source="test")
    assert store.path.read_text() == "broken"


def test_hook_uses_bound_omnigent_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    observe_hook(
        "conv_owned",
        {
            "session_id": "provider-session",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create"},
            "tool_response": {"stdout": A},
            "tool_use_id": "tool_123",
        },
    )
    assert [entry.url for entry in SessionPrRegistry("conv_owned").list()] == [A]
    assert SessionPrRegistry("provider-session").list() == []


@pytest.mark.parametrize(
    "command,result,urls",
    [
        (f'gh pr edit -R example/two 42 --body "{A}"', "Updated", [B]),
        (f'gh pr edit --body "{A}"', "Updated", []),
        ("gh pr view || gh pr create", A, []),
        ("gh pr create", {"stdout": A, "metadata": {"exit_code": 1}}, []),
        ("gh pr create", {"output": A, "session_id": 12, "exit_code": None}, []),
        ("gh pr comment -R example/one 42 -b test", "Posted", [A]),
    ],
)
def test_ambiguous_and_nonterminal_commands(command: str, result: object, urls: list[str]) -> None:
    refs, _ = extract_prs("Bash", {"command": command}, result)
    assert [ref.url for ref in refs] == urls


def test_mixed_operations_do_not_claim_creation() -> None:
    refs, created = extract_prs("Bash", {"command": "gh pr create; gh pr edit 42"}, A)
    assert [ref.url for ref in refs] == [A]
    assert not created


def test_rest_proxy_wrapper() -> None:
    refs, created = extract_prs(
        "mcp__custom__github_write_api_call",
        {"endpoint": "pull_requests.create", "params": {"org": "example", "repo": "one"}},
        {"result": {"html_url": A, "body": B}},
    )
    assert [ref.url for ref in refs] == [A]
    assert created


@pytest.mark.parametrize(
    "result",
    [
        json.dumps({"result": _MCP_CREATE_SUMMARY}),
        {"result": _MCP_CREATE_SUMMARY},
        {"structuredContent": {"result": _MCP_CREATE_SUMMARY}},
        {
            "content": [{"type": "text", "text": json.dumps({"result": _MCP_CREATE_SUMMARY})}],
            "structuredContent": {"result": _MCP_CREATE_SUMMARY},
        },
        A,
        {"result": f"Draft ready: [open pull request]({A})."},
        {"content": [{"type": "text", "text": f"Brouillon disponible : <{A}>"}]},
        {"result": f"Related repository: {B}\nYour pull request: {A}"},
        {"result": {"number": 42}},
        json.dumps({"result": json.dumps({"number": 42, "body": B})}),
    ],
)
def test_write_proxy_create_identity_is_independent_of_wording(result: object) -> None:
    refs, created = extract_prs(
        "mcp__github__github_write_api_call",
        {
            "endpoint": "pull_requests.create",
            "params": {"org": "example", "repo": "one", "branch": "user/topic", "draft": True},
        },
        result,
    )
    assert [ref.url for ref in refs] == [A]
    assert created


@pytest.mark.parametrize(
    "result",
    [
        {"result": _MCP_CREATE_SUMMARY.replace(A, B)},
        {"result": f"Could not create PR. Related PR: {A}", "isError": True},
        {"result": _MCP_CREATE_SUMMARY, "isError": True},
        {"result": _MCP_CREATE_SUMMARY, "success": False},
        {"body": _MCP_CREATE_SUMMARY},
        {"result": {"body": _MCP_CREATE_SUMMARY}},
        {"result": json.dumps({"body": A})},
        {"result": f"Two PRs: {A} and {A.replace('/42', '/99')}"},
        {"result": "https://github.com/example/one/issues/42"},
    ],
)
def test_write_proxy_rejects_failed_or_ambiguous_results(result: object) -> None:
    refs, _ = extract_prs(
        "mcp__github__github_write_api_call",
        {"endpoint": "pull_requests.create", "params": {"org": "example", "repo": "one"}},
        result,
    )
    assert refs == []


@pytest.mark.parametrize("endpoint", ["pull_requests.list", "issues.create", "unknown"])
def test_write_proxy_requires_recognized_pr_operation(endpoint: str) -> None:
    refs, _ = extract_prs(
        "mcp__github__github_write_api_call",
        {"endpoint": endpoint, "params": {"org": "example", "repo": "one"}},
        {"result": _MCP_CREATE_SUMMARY},
    )
    assert refs == []


@pytest.mark.parametrize(
    "name,args,result,urls,created",
    [
        (
            "mcp__github__github_write_api_call",
            {
                "endpoint": "pull_requests.update",
                "params": {"org": "example", "repo": "one", "pull_number": 42},
            },
            "Done.",
            [A],
            False,
        ),
        (
            "mcp__custom__update_pull_request",
            {"owner": "example", "repo": "one", "pullNumber": 42},
            {"url": B, "result": f"Related: {B}"},
            [A],
            False,
        ),
        (
            "mcp__custom__update_pull_request",
            {"owner": "example", "repo": "one", "pullNumber": 42},
            {"isError": True},
            [],
            False,
        ),
        (
            "mcp__custom__update_pull_request",
            {"owner": "example", "repo": "one", "pullNumber": 42},
            {"html_url": A.replace("github.com", "github.example.org")},
            [A.replace("github.com", "github.example.org")],
            False,
        ),
        (
            "mcp__custom__create_pull_request",
            {"owner": "example", "repo": "one"},
            {"content": [{"type": "text", "text": f"Ready! {A}"}]},
            [A],
            True,
        ),
        (
            "mcp__custom__create_pull_request",
            {"owner": "example", "repo": "one"},
            {"html_url": A, "result": f"Based on {A.replace('/42', '/99')}"},
            [A],
            True,
        ),
        (
            "mcp__custom__create_pull_request",
            {"owner": "example", "repo": "one", "body": A},
            "Done.",
            [],
            True,
        ),
        (
            "mcp__custom__list_pull_requests",
            {"owner": "example", "repo": "one"},
            f"Found: {A}",
            [],
            False,
        ),
    ],
)
def test_mcp_structured_identity_and_url_fallback(
    name: str, args: dict, result: object, urls: list[str], created: bool
) -> None:
    refs, was_created = extract_prs(name, args, result)
    assert [ref.url for ref in refs] == urls
    assert was_created is created


@pytest.mark.parametrize("already_created", [False, True])
def test_mcp_update_recovers_missed_pr_without_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_created: bool
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    registry = SessionPrRegistry("conv_recovery")
    if already_created:
        registry.record([PullRequestRef.from_url(A)], relationship="created", source="test")

    def update(repo: str, call_id: str) -> None:
        observe_hook(
            "conv_recovery",
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "mcp__github__github_write_api_call",
                "tool_input": {
                    "endpoint": "pull_requests.update",
                    "params": {"org": "example", "repo": repo, "pull_number": 42},
                },
                "tool_response": {"result": "Done."},
                "tool_use_id": call_id,
            },
        )

    for call_id in ("update-1", "update-1", "update-2"):
        update("one", call_id)
    entries = registry.list()
    assert [entry.url for entry in entries] == [A]
    assert entries[0].relationship == ("created" if already_created else "worked_on")

    update("two", "update-3")
    assert {entry.url for entry in registry.list()} == {A, B}


@pytest.mark.parametrize("failed", [False, True])
async def test_runner_shell_dispatch_records_only_success(tmp_path, monkeypatch, failed) -> None:
    from omnigent.runner import tool_dispatch

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    output = A + ("\n[exit code: 1]" if failed else "\n")

    async def shell(*_args, **_kwargs):
        return output

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", shell)
    result = await tool_dispatch.execute_tool(
        tool_name="sys_os_shell",
        arguments=json.dumps({"command": "gh pr create"}),
        conversation_id="conv_dispatch",
        agent_spec=None,
    )
    assert result == output
    assert [pr.url for pr in SessionPrRegistry("conv_dispatch").list()] == ([] if failed else [A])


@pytest.mark.parametrize(
    "command,urls",
    [
        ("gh issue create --title pr edit --body ignored", []),
        ("gh -R example/two pr edit 42 --title new", [B]),
        (
            "GH_HOST=github.example.org gh pr edit 42 -R example/one",
            [A.replace("github.com", "github.example.org")],
        ),
        ("gh pr create || true", []),
    ],
)
def test_gh_subcommand_and_host(command: str, urls: list[str]) -> None:
    refs, _ = extract_prs("Bash", {"command": command}, A if not urls else "Updated")
    assert [pr.url for pr in refs] == urls


@pytest.mark.parametrize(
    "result", [{"backgroundTaskId": "job-1"}, {"status": "running"}, {"interrupted": True}]
)
def test_background_or_interrupted_shell_does_not_attach_target(result: dict) -> None:
    refs, _ = extract_prs("Bash", {"command": f"gh pr edit {A} --title new"}, result)
    assert refs == []


def test_read_api_output_is_not_attributed_to_later_create() -> None:
    refs, _ = extract_prs(
        "Bash",
        {"command": "gh api repos/example/one/pulls/42 --jq .html_url; gh pr create"},
        A + "\n" + B,
    )
    assert refs == []


@pytest.mark.parametrize("envelope", [False, True])
def test_rest_create_with_jq_and_multiline_shell(envelope: bool) -> None:
    url = "https://github.com/example/project-dev/pull/123"
    output = f"HEAD SHA: {'a' * 40}\n{url}\n"
    result = {"stdout": output, "exit_code": 0} if envelope else output
    refs, created = extract_prs("Bash", {"command": _REST_CREATE_COMMAND}, result)
    assert [pr.url for pr in refs] == [url]
    assert created


@pytest.mark.parametrize(
    "command,result,urls,created",
    [
        ("gh api /repos/example/one/pulls -X POST --jq .html_url", A, [A], True),
        (
            "gh api /repos/example/one/pulls -X POST --jq .html_url",
            {"stdout": A, "output": A, "exit_code": 0},
            [A],
            True,
        ),
        ("gh api -XPOST --jq=.html_url repos/example/one/pulls", A, [A], True),
        ("gh api /repos/example/one/pulls -f title=test -q .html_url", A, [A], True),
        (
            "gh api /repos/example/one/pulls --method POST",
            {"stdout": json.dumps({"html_url": A})},
            [A],
            True,
        ),
        ("gh api /repos/example/one/pulls/42 -X PATCH --jq .html_url", A, [A], False),
        ("gh api /repos/example/one/pulls/42 --jq .html_url", A, [], False),
        ("gh api /repos/example/one/pulls -X GET -f title=test --jq .html_url", A, [], False),
        ("gh api /repos/example/one/pulls -X POST --jq .body", B, [], False),
        ("gh api /repos/example/one/pulls -X POST --jq .html_url", B, [], False),
        ("gh api /repos/example/one/pulls/99 -X PATCH --jq .html_url", A, [], False),
        (
            "gh api --input /repos/example/one/pulls -X POST "
            "/repos/example/one/issues --jq .html_url",
            A,
            [],
            False,
        ),
        (
            "gh api /repos/example/one/pulls -X POST --jq .html_url",
            {"stdout": A, "exit_code": 1},
            [],
            False,
        ),
    ],
)
def test_rest_url_projection(command: str, result: object, urls: list[str], created: bool) -> None:
    refs, was_created = extract_prs("Bash", {"command": command}, result)
    assert [pr.url for pr in refs] == urls
    if urls:
        assert was_created is created


@pytest.mark.parametrize("quote,urls", [('"', [A]), ("'", [])])
def test_shell_continuation_respects_quoting(quote: str, urls: list[str]) -> None:
    command = f"gh api {quote}/repos/example/one/pul\\\nls{quote} -X POST --jq .html_url"
    refs, _ = extract_prs("Bash", {"command": command}, A)
    assert [pr.url for pr in refs] == urls
