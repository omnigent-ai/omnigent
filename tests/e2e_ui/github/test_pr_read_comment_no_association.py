"""PR reads and comments must not create session PR associations.

Session PR tracking observes completed shell calls on the runner
(``omnigent/runner/pr_observer.py``) and surfaces associated PRs in the
web UI: the workspace rail's GitHub tab shows a "Session pull request"
picker and the composer status line links the selected PR. Reported bug:
tool completions that merely *read* a pull request (``gh pr view``, a
``gh api`` GET) or *comment* on one (``gh pr comment``) associate that PR
with the session, so unrelated review activity pollutes the session's PR
selector. Only creation, edits, merges, and approvals should be tracked.

These tests drive the real journey end to end — a live server + runner
executes the agent's shell command for real (a PATH-stubbed ``gh`` prints
gh's canonical output for each subcommand, so no GitHub access is needed;
the observer only ever sees the command string and its output) — and
assert the *correct* behavior: a session that only reads or comments on a
PR has no PR associations afterwards. The three read/comment shapes fail
on the affected build (the picker lists ``example/one #42``); the
``gh pr create`` control passes, pinning that the fix must suppress
reads/comments without also suppressing creation tracking.
"""

from __future__ import annotations

import gzip
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    configure_mock_llm,
    open_right_rail,
    set_fallback_mock_llm,
)

_PR_URL = "https://github.com/example/one/pull/42"
_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# The per-fixture model gives each test an isolated mock-LLM queue.
_AGENT_YAML = """\
name: {name}
prompt: |
  You are a deterministic test assistant. When asked about a pull request
  you run a shell command against it, then confirm.

executor:
  model: {model}
  harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""

# Stands in for the real gh CLI on PATH: prints gh's canonical output for
# each subcommand the tests exercise — the PR URL for ``pr view`` and
# ``pr create``, the comment permalink for ``pr comment``, and the pull
# request JSON for an ``api`` read — and succeeds, exactly what a real
# successful call shows.
_GH_STUB = f"""\
#!/bin/sh
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  echo "{_PR_URL}"
elif [ "$1" = "pr" ] && [ "$2" = "comment" ]; then
  echo "{_PR_URL}#issuecomment-1"
elif [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  echo "{_PR_URL}"
elif [ "$1" = "api" ]; then
  echo '{{"url": "https://api.github.com/repos/example/one/pulls/42", \
"html_url": "{_PR_URL}", "number": 42, "state": "open"}}'
fi
exit 0
"""

# The reported read/comment shapes that must not associate the PR, plus the
# creation shape that must keep associating it. The leading PATH export only
# makes the stubbed gh resolvable; it adds no gh clause.
_PREFIX = 'export PATH="{stub}:$PATH"; cd {worktree} && '
_READ_COMMENT_COMMANDS = {
    "read-pr-view": _PREFIX + "gh pr view 42 -R example/one",
    "comment-pr-comment": _PREFIX + "gh pr comment 42 -R example/one --body 'Looks good'",
    "read-rest-api-get": _PREFIX + "gh api repos/example/one/pulls/42",
}
_CREATE_COMMAND = _PREFIX + "gh pr create --title 'Example' --body 'Example'"


def _agent_bundle(name: str, model: str, cwd: str) -> bytes:
    """Gzip-tar the agent YAML for multipart upload."""
    yaml_text = _AGENT_YAML.format(name=name, model=model, cwd=cwd)
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = yaml_text.encode()
        info = tarfile.TarInfo(name=f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


@pytest.fixture
def pr_probe_runner_id(
    live_server: str,
    runner_id: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Recover the shared runner after an earlier crash test in the shard."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        yield runner_id
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


@pytest.fixture
def pr_probe_session(
    live_server: str,
    pr_probe_runner_id: str,
    mock_llm_server_url: str,
) -> Iterator[tuple[str, str, str, Path, Path]]:
    """An isolated runner-bound session whose workspace can run the commands.

    The workspace holds a ``stub-bin/gh`` for PATH and a ``worktree`` git
    repo, so the agent's command runs from a realistic checkout without any
    GitHub access.
    """
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-pr-read-comment-"))
    stub = ws / "stub-bin"
    stub.mkdir()
    (stub / "gh").write_text(_GH_STUB)
    (stub / "gh").chmod(0o755)
    worktree = ws / "worktree"
    _git("init", "-q", "-b", "topic", str(worktree))
    _git(
        "-C",
        str(worktree),
        "-c",
        "user.email=e2e@example.com",
        "-c",
        "user.name=e2e",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "init",
    )

    name = f"pr_assoc_probe_{uuid.uuid4().hex[:8]}"
    model = f"pr-assoc-probe-{uuid.uuid4().hex[:8]}"
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _agent_bundle(name, model, str(ws)),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    try:
        httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": pr_probe_runner_id},
            timeout=10.0,
        ).raise_for_status()
        yield (live_server, session_id, model, stub, worktree)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(ws, ignore_errors=True)


def _drive_shell_turn(
    page: Page,
    base_url: str,
    session_id: str,
    model: str,
    mock_url: str,
    command: str,
    prompt: str,
    reply: str,
) -> None:
    """Run one agent turn whose only tool call executes *command* for real."""
    # Configure both responses together because reconfiguring a queue resets it.
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_gh",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": command}),
                    }
                ]
            },
            {"text": reply},
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_url, model, "Done.")

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).last).to_contain_text(reply, timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)


def _open_github_panel(page: Page) -> Locator:
    """Reload like a returning user, open the rail, and select the GitHub tab."""
    page.reload()
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="GitHub").click()
    # The "Link a PR" affordance renders once the panel's info request has
    # settled (with or without tracked PRs), so waiting on it keeps negative
    # assertions from passing vacuously against a still-loading panel.
    expect(rail.get_by_role("button", name="Link a PR").first).to_be_visible(timeout=30_000)
    return rail


@pytest.mark.parametrize("shape", list(_READ_COMMENT_COMMANDS), ids=list(_READ_COMMENT_COMMANDS))
def test_read_or_comment_does_not_associate_pr(
    page: Page,
    pr_probe_session: tuple[str, str, str, Path, Path],
    mock_llm_server_url: str,
    shape: str,
) -> None:
    """Reading or commenting on a PR leaves the session without associations."""
    base_url, session_id, model, stub, worktree = pr_probe_session
    command = _READ_COMMENT_COMMANDS[shape].format(stub=stub, worktree=worktree)
    _drive_shell_turn(
        page,
        base_url,
        session_id,
        model,
        mock_llm_server_url,
        command,
        "Take a look at PR 42 in example/one.",
        "Reviewed the pull request.",
    )

    rail = _open_github_panel(page)
    # A read/comment must leave the session unassociated: no session-PR
    # picker in the GitHub tab and no composer status-line PR link.
    expect(rail.get_by_role("combobox", name="Session pull request")).to_have_count(0)
    expect(page.get_by_test_id("composer-pr-link")).to_have_count(0)
    # The registry itself must stay empty — a hidden association would still
    # resurface in pickers and defaults later.
    info = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/github",
        timeout=30.0,
    )
    info.raise_for_status()
    prs = info.json().get("prs", [])
    assert prs == [], f"read/comment associated PRs: {[pr['url'] for pr in prs]}"


def test_created_pr_remains_tracked(
    page: Page,
    pr_probe_session: tuple[str, str, str, Path, Path],
    mock_llm_server_url: str,
) -> None:
    """Control: ``gh pr create`` still associates its PR with the session."""
    base_url, session_id, model, stub, worktree = pr_probe_session
    command = _CREATE_COMMAND.format(stub=stub, worktree=worktree)
    _drive_shell_turn(
        page,
        base_url,
        session_id,
        model,
        mock_llm_server_url,
        command,
        "Open a pull request for this change.",
        "Opened the pull request.",
    )

    rail = _open_github_panel(page)
    picker = rail.get_by_role("combobox", name="Session pull request")
    expect(picker).to_have_text("example/one #42", timeout=30_000)
    expect(page.get_by_test_id("composer-pr-link")).to_have_accessible_name("#42")
