"""E2E: the composer branch chip reports the workspace's checked-out branch.

The gray bar above the session composer has a branch chip
(``data-testid="composer-git-branch"``). For a session whose bound workspace
is a real git checkout sitting on a named branch, the chip renders
``No branch reported`` — the session's ``git_branch`` is only ever persisted
when the create request carries explicit git options (a server-created
worktree, or an ``existing_worktree`` bind), and no server or runner path
derives it from the workspace's actual ``git branch --show-current``. The
value also never refreshes after session start, so the chip stays stuck at
the empty state for the whole session.

This drives the real user journey end to end — a runner-bound session whose
stored workspace is a git checkout on a named branch, the session page opened
in the SPA, one full turn run so the runner has every opportunity to report
the branch — with no request interception. The assertion encodes the CORRECT
behavior: a workspace on a named branch must surface that branch name in the
chip ("No branch reported" is reserved for a genuinely detached HEAD or a
non-git workspace). On a build with the bug the chip still reads
``No branch reported`` after the turn and this test fails there — the
regression guard for the fix.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _build_hello_world_bundle,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
)

# The branch the reporter's workspace was on; any named branch reproduces.
_BRANCH = "codex/fix-resume-mcp-startup-events"
# Unique workspace dir name so the workspace chip is unambiguous in the bar.
_REPO_DIRNAME = "branch-chip-workspace"

_COMPOSER_LABEL = "Message the agent"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# Stable substring routing this test's turn to its mock reply.
_PROMPT = "Say hello and nothing else. (branch-chip journey)"


def _git(repo: Path, *args: str) -> None:
    """Run a git command inside *repo*, failing loudly on error."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def branch_workspace_session(
    live_server: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A runner-bound session whose workspace is a git checkout on a branch.

    Recreates the reporter-machine shape: a plain git repository (not a
    worktree, not detached, not bare) with a named branch checked out, bound
    as the session workspace via ``metadata.workspace`` — the everyday
    "open a session in my repo" journey, with no git options on the create
    request.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path: Per-test dir for the git checkout.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id, workspace_path)``.
    """
    repo = tmp_path / _REPO_DIRNAME
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "e2e@example.com")
    _git(repo, "config", "user.name", "E2E")
    (repo / "README.md").write_text("branch chip journey\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "checkout", "-b", _BRANCH)

    # Precondition from the report: the workspace really is on the branch.
    shown = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert shown == _BRANCH, f"workspace is on {shown!r}, expected {_BRANCH!r}"

    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    bundle = _build_hello_world_bundle()
    create = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(repo)})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    patch = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch.raise_for_status()
    try:
        yield (live_server, session_id, repo)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


def test_branch_chip_reports_checked_out_branch(
    page: Page,
    branch_workspace_session: tuple[str, str, Path],
    mock_llm_server_url: str,
) -> None:
    """The branch chip shows the workspace's branch, not "No branch reported"."""
    base_url, session_id, _repo = branch_workspace_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "hello"}],
        key="branch-chip",
        match="branch-chip journey",
    )

    page.goto(f"{base_url}/c/{session_id}")

    # The gray bar above the composer, with the workspace chip proving the
    # session is bound to the git checkout (label = the repo dir basename).
    bar = page.get_by_test_id("composer-workspace-controls")
    expect(bar).to_be_visible(timeout=30_000)
    expect(bar.get_by_text(_REPO_DIRNAME, exact=True)).to_be_visible(timeout=30_000)

    # Run one full turn so the runner has every opportunity to observe the
    # workspace and report its branch — the chip must not stay stuck at the
    # session-start empty state.
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(_PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # Open the chip's popover so the reported value (or its absence) is on
    # screen, then assert the chip label itself.
    chip = page.get_by_test_id("composer-git-branch")
    expect(chip).to_be_visible(timeout=30_000)
    chip.click()

    # CORRECT behavior: the workspace is on a named branch, so the chip must
    # surface that branch name. On the broken build the chip still reads
    # "No branch reported" here, which is exactly what this assertion trips on.
    expect(chip).to_contain_text(_BRANCH, timeout=20_000)
