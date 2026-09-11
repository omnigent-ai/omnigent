"""A mixed read/comment + create shell call must keep tracking the created PR.

Session PR tracking observes completed shell calls on the runner
(``omnigent/runner/pr_observer.py``) and surfaces associated PRs in the
web UI: the workspace rail's GitHub tab shows a "Session pull request"
picker and the composer status line links the selected PR. Reported bug: a
single shell tool call that combines an excluded PR read/comment with PR
creation — e.g. ``gh pr comment 42 ... && gh pr create ...`` — loses the
created PR. The read/comment is deliberately excluded from tracking, but the
observer then refuses to attribute any URL from the shared stdout, so the
newly created PR never becomes a session association: the GitHub tab shows
no "Session pull request" picker and the composer never links the new PR.

These tests drive the real journey end to end — a live server + runner
executes the agent's shell command for real (a PATH-stubbed ``gh`` prints
gh's canonical output for each subcommand: the comment permalink for
``pr comment`` and the bare PR URL for ``pr create``, so no GitHub access
is needed; the observer only ever sees the command string and its output)
— and assert the *correct* behavior: the created PR (#43) is associated
with the session while the merely-commented PR (#42) is not. On the
affected build both parametrized orders fail: the session ends up with no
PR associations at all, so the picker never appears.
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

_READ_PR_URL = "https://github.com/example/one/pull/42"
_CREATED_PR_URL = "https://github.com/example/one/pull/43"
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
# each subcommand the tests exercise — the comment permalink (with its
# ``#issuecomment-…`` fragment) for ``pr comment`` and the bare new-PR URL
# for ``pr create`` — and succeeds, exactly what a real successful call
# shows.
_GH_STUB = f"""\
#!/bin/sh
if [ "$1" = "pr" ] && [ "$2" = "comment" ]; then
  echo "{_READ_PR_URL}#issuecomment-1"
elif [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  echo "{_CREATED_PR_URL}"
fi
exit 0
"""

# The reported mixed shapes: one shell tool call carrying both an excluded
# comment and a PR creation, in both orders. The leading PATH export only
# makes the stubbed gh resolvable; it adds no gh clause.
_PREFIX = 'export PATH="{stub}:$PATH"; cd {worktree} && '
_MIXED_COMMANDS = {
    "comment-then-create": _PREFIX
    + "gh pr comment 42 -R example/one --body 'Reviewed.' && "
    + "gh pr create --title 'Test' --body 'Test'",
    "create-then-comment": _PREFIX
    + "gh pr create --title 'Test' --body 'Test' && "
    + "gh pr comment 42 -R example/one --body 'Reviewed.'",
}


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
def mixed_pr_runner_id(
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
def mixed_pr_session(
    live_server: str,
    mixed_pr_runner_id: str,
    mock_llm_server_url: str,
) -> Iterator[tuple[str, str, str, Path, Path]]:
    """An isolated runner-bound session whose workspace can run the commands.

    The workspace holds a ``stub-bin/gh`` for PATH and a ``worktree`` git
    repo, so the agent's command runs from a realistic checkout without any
    GitHub access.
    """
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-pr-mixed-create-"))
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

    name = f"pr_mixed_probe_{uuid.uuid4().hex[:8]}"
    model = f"pr-mixed-probe-{uuid.uuid4().hex[:8]}"
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
            json={"runner_id": mixed_pr_runner_id},
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
    # settled (with or without tracked PRs), so waiting on it keeps the
    # assertions from racing a still-loading panel.
    expect(rail.get_by_role("button", name="Link a PR").first).to_be_visible(timeout=30_000)
    return rail


@pytest.mark.parametrize("order", list(_MIXED_COMMANDS), ids=list(_MIXED_COMMANDS))
def test_mixed_comment_and_create_tracks_created_pr(
    page: Page,
    mixed_pr_session: tuple[str, str, str, Path, Path],
    mock_llm_server_url: str,
    order: str,
) -> None:
    """One shell call mixing a comment with ``pr create`` keeps the new PR.

    The commented PR (#42) stays excluded, but the created PR (#43) must
    become the session's pull request: named by the GitHub tab's picker,
    linked from the composer status line, and present in the session's PR
    registry. On the affected build the created PR is lost entirely — the
    session has no PR associations and the picker never renders.
    """
    base_url, session_id, model, stub, worktree = mixed_pr_session
    command = _MIXED_COMMANDS[order].format(stub=stub, worktree=worktree)
    _drive_shell_turn(
        page,
        base_url,
        session_id,
        model,
        mock_llm_server_url,
        command,
        "Reply to PR 42 in example/one, then open a pull request for this change.",
        "Commented and opened the pull request.",
    )

    rail = _open_github_panel(page)
    # The created PR must be the session's pull request in the UI.
    picker = rail.get_by_role("combobox", name="Session pull request")
    expect(picker).to_have_text("example/one #43", timeout=30_000)
    expect(page.get_by_test_id("composer-pr-link")).to_have_accessible_name("#43")
    # The registry must hold exactly the creation: #43 associated, and the
    # merely-commented #42 not resurrected by the shared stdout.
    info = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/github",
        timeout=30.0,
    )
    info.raise_for_status()
    urls = [pr["url"] for pr in info.json().get("prs", [])]
    assert _CREATED_PR_URL in urls, f"created PR lost from session associations: {urls}"
    assert _READ_PR_URL not in urls, f"commented PR wrongly associated: {urls}"
