"""The PR for a branch the agent pushed appears in the session's Pull Requests tab.

The agent pushes branch ``topic`` of a ``git@github.com:contributor/project.git``
checkout whose open PR is #42. Git really pushes, over an ``core.sshCommand`` that
serves a local bare repository, and a PATH-first ``gh`` answers GitHub. The
session workspace is a non-git parent, so only push tracking can show the PR.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.github_remote import git, github_checkout
from tests._helpers.server_runner import server_runner
from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.github._gh_shim import (
    create_session,
    open_pull_requests_tab,
    stack_env,
    write_gh_shim,
)

REPO = "contributor/project"
BRANCH = "topic"
PR_URL = f"https://github.com/{REPO}/pull/42"
PR = {
    "number": 42,
    "url": PR_URL,
    "title": "Topic change",
    "state": "OPEN",
    "isDraft": False,
    "author": {"login": "contributor"},
    "baseRefName": "main",
    "headRefName": BRANCH,
    "headRefOid": "a" * 40,
    "baseRefOid": "b" * 40,
    "body": "",
    "comments": [],
    "statusCheckRollup": [],
}
REPLY = "Pushed the change."

GITHUB_MCP = """import json, sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP

server = FastMCP("github", host="127.0.0.1", port=int(sys.argv[1]))
calls = Path(sys.argv[2])


@server.tool()
def push_files(owner: str, repo: str, branch: str, files: list[dict], message: str) -> dict:
    with calls.open("a") as log:
        print(json.dumps({"owner": owner, "repo": repo, "branch": branch}), file=log)
    sha = "e" * 40
    return {
        "ref": f"refs/heads/{branch}",
        "url": f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
        "object": {"sha": sha, "type": "commit",
                   "url": f"https://api.github.com/repos/{owner}/{repo}/git/commits/{sha}"},
    }


server.run(transport="streamable-http")
"""

PUSHES = {
    "literal-push": "git push origin HEAD:topic",
    "alias-with-pipe": "git pp 2>&1 | grep -v Bloom | tail -15",
    "make-wrapper": "make push",
    "redirected-push": "git push origin HEAD:topic 2>&1",
    "quiet-push": "git push -q -u origin topic",
}
GH_WRITES = {
    "pr-ready-without-repo": "gh pr ready 42",
    "gh-api-contents-write": (
        f"gh api -X PUT repos/{REPO}/contents/notes.txt"
        " -f message='Update notes' -f content=bm90ZXMK -f branch=topic"
    ),
}


@pytest.fixture(scope="module")
def push_stack(
    built_spa: None, mock_llm_server_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[str, str, Path]]:
    root = tmp_path_factory.mktemp("pr-push")
    binary = root / "bin"
    write_gh_shim(binary, repo=REPO, pr=PR)
    runtime = root / "runtime"
    runtime.mkdir()
    env = stack_env(binary, runtime, mock_llm_server_url)
    with server_runner(runtime, base_env=env) as stack:
        stack.start_runner()
        yield stack.base_url, stack.runner_id, binary


@pytest.fixture
def checkout(push_stack: tuple[str, str, Path], tmp_path: Path) -> tuple[Path, Path, Path]:
    """A worktree on ``topic`` already pushed once, with one new commit to push."""
    _, _, binary = push_stack
    (binary / "gh-calls.log").write_text("")
    workspace = tmp_path / "workspace"
    worktree = workspace / "worktree"
    remote = github_checkout(worktree, tmp_path / "github", REPO, BRANCH)
    git("config", "alias.pp", "push origin HEAD", cwd=worktree)
    (worktree / "Makefile").write_text("push:\n\tgit push origin HEAD\n")
    (worktree / "notes.txt").write_text("notes\n")
    git("add", "Makefile", "notes.txt", cwd=worktree)
    git("commit", "-qm", "Update notes", cwd=worktree)
    return workspace, worktree, remote


@pytest.fixture
def github_mcp(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A streamable-HTTP stand-in for the GitHub MCP server's ``push_files``."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    calls = tmp_path / "mcp-calls.log"
    calls.write_text("")
    proc = subprocess.Popen(
        [sys.executable, "-c", GITHUB_MCP, str(port), str(calls)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "readiness-probe", "version": "0"},
        },
    }
    try:
        deadline = time.monotonic() + 30
        with httpx.Client(trust_env=False) as client:
            while True:
                assert proc.poll() is None, f"MCP stub exited with {proc.returncode}"
                try:
                    accept = {"Accept": "application/json, text/event-stream"}
                    if client.post(url, json=initialize, headers=accept).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "MCP stub never became ready"
                time.sleep(0.2)
        yield url, calls
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _script_turn(mock_llm_server_url: str, model: str, name: str, arguments: dict) -> None:
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {"call_id": "call_push", "name": name, "arguments": json.dumps(arguments)}
                ]
            },
            {"text": REPLY},
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, "Done.")


def _hold_for_recording(page: Page, milliseconds: int) -> None:
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(milliseconds)


def _push_and_open_pull_requests(
    page: Page, base_url: str, session_id: str, tool_text: str
) -> Locator:
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    _hold_for_recording(page, 2_000)
    composer.fill("Push my latest commit to the PR branch.")
    _hold_for_recording(page, 2_000)
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
    expect(assistant.last).to_contain_text(REPLY, timeout=60_000)
    expect(page.locator('[data-testid="working-indicator"]')).to_have_count(0, timeout=60_000)
    expect(assistant.last.get_by_text(REPLY)).to_be_in_viewport()
    # Fold, run summary, then the tool's own row.
    triggers = page.get_by_test_id("turn-worked-fold").locator('[data-slot="collapsible-trigger"]')
    for index in range(3):
        triggers.nth(index).click()
        expect(triggers.nth(index)).to_have_attribute("aria-expanded", "true")
    shown = page.get_by_test_id("turn-worked-fold").get_by_text(tool_text).last
    shown.scroll_into_view_if_needed()
    expect(shown).to_be_in_viewport(ratio=1)
    _hold_for_recording(page, 5_000)
    rail = open_pull_requests_tab(page, base_url, session_id)
    expect(rail.get_by_role("heading", name="Pull Requests")).to_be_in_viewport()
    return rail


def _gh_calls(binary: Path) -> list[dict]:
    calls = [json.loads(line) for line in (binary / "gh-calls.log").read_text().splitlines()]
    # The Pull Requests panel's own detail reads are not part of push tracking.
    return [
        call
        for call in calls
        if call["args"][:2] not in (["auth", "status"], ["pr", "view"])
        and "graphql" not in call["args"]
    ]


def _expect_pr_listed(
    page: Page, rail: Locator, base_url: str, session_id: str, observed: Callable[[], dict]
) -> None:
    try:
        expect(rail.locator(f'a[href="{PR_URL}"]').first).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("composer-pr-link")).to_have_accessible_name("#42")
        _hold_for_recording(page, 3_000)
    finally:
        listed = httpx.get(f"{base_url}/v1/sessions/{session_id}/pull-requests", timeout=10)
        print(
            json.dumps({**observed(), "session_id": session_id, "listed": listed.json()["data"]})
        )


@pytest.mark.parametrize("shape", [*PUSHES, *GH_WRITES])
def test_pushed_branch_pr_appears_in_pull_requests_tab(
    request: pytest.FixtureRequest,
    push_stack: tuple[str, str, Path],
    checkout: tuple[Path, Path, Path],
    mock_llm_server_url: str,
    shape: str,
) -> None:
    base_url, runner_id, binary = push_stack
    workspace, worktree, remote = checkout
    write = PUSHES.get(shape) or GH_WRITES[shape]
    command = f"cd {shlex.quote(str(worktree))} && {write}"
    model = f"pr-push-{uuid.uuid4().hex[:8]}"
    session_id = create_session(base_url, runner_id, workspace, model=model)
    _script_turn(mock_llm_server_url, model, "sys_os_shell", {"command": command})
    page: Page = request.getfixturevalue("page")
    rail = _push_and_open_pull_requests(page, base_url, session_id, write)
    if shape in PUSHES:
        assert git("rev-parse", BRANCH, cwd=remote) == git("rev-parse", "HEAD", cwd=worktree)
    else:
        ran = [c for c in _gh_calls(binary) if c["args"][:2] in (["pr", "ready"], ["api", "-X"])]
        assert ran and ran[0]["rc"] == 0, ran
    _expect_pr_listed(
        page,
        rail,
        base_url,
        session_id,
        lambda: {
            "shape": shape,
            "command": command,
            "gh_calls": _gh_calls(binary),
            "remote_reflog": git("reflog", "show", "--format=%h %gs", BRANCH, cwd=remote),
        },
    )


def test_mcp_push_files_pr_appears_in_pull_requests_tab(
    request: pytest.FixtureRequest,
    push_stack: tuple[str, str, Path],
    checkout: tuple[Path, Path, Path],
    github_mcp: tuple[str, Path],
    mock_llm_server_url: str,
) -> None:
    base_url, runner_id, binary = push_stack
    workspace, _, _ = checkout
    url, mcp_calls = github_mcp
    model = f"pr-push-{uuid.uuid4().hex[:8]}"
    tools = f"tools:\n  github:\n    type: mcp\n    url: {url}\n"
    session_id = create_session(base_url, runner_id, workspace, model=model, tools=tools)
    arguments = {
        "owner": "contributor",
        "repo": "project",
        "branch": BRANCH,
        "files": [{"path": "notes.txt", "content": "notes\n"}],
        "message": "Update notes",
    }
    _script_turn(mock_llm_server_url, model, "github__push_files", arguments)
    page: Page = request.getfixturevalue("page")
    rail = _push_and_open_pull_requests(page, base_url, session_id, "Push files")
    assert mcp_calls.read_text().strip(), "push_files never ran"
    _expect_pr_listed(
        page,
        rail,
        base_url,
        session_id,
        lambda: {
            "shape": "mcp-push-files",
            "mcp_calls": mcp_calls.read_text().splitlines(),
            "gh_calls": _gh_calls(binary),
        },
    )
