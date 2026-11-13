"""E2E: the Changes tab recovers once a new session's runner binds.

A new sandbox session is visible before its runner is bound, and the
environment GET fails with a 409 "not bound to a runner" in that window.
After the bind, the Changes tab must load without needing another turn.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.live_server import terminate_process
from tests._helpers.native_session import create_native_session
from tests._helpers.session import bind_session_runner
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    open_right_rail,
    reset_mock_llm,
)
from tests.helpers.ui_configuration import validate_native_claude_mock_routing

_TERMINAL_READY_TIMEOUT_MS = 120_000
# Long enough for one refetch after the bind, inside the query's 60s staleTime.
_RECOVERY_WINDOW_MS = 20_000


def _git_workspace(root: Path) -> Path:
    workspace = root / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    (workspace / "README.md").write_text("scratch repository\n")
    git = ["git", "-C", str(workspace), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", "README.md"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    # Skip the startup cache probe; its temporary directory can appear in Changes.
    subprocess.run([*git, "config", "core.untrackedCache", "true"], check=True)
    return workspace


@pytest.fixture
def unbound_native_claude_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    claude_managed_settings: None,
) -> Iterator[tuple[str, str, str]]:
    """A claude-native session in a git workspace, created but not yet runner-bound."""
    if shutil.which("claude") is None:
        pytest.skip("claude CLI is required for the native Claude Code journey")
    validate_native_claude_mock_routing(mock_llm_server_url)
    workspace = _git_workspace(tmp_path)
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id: str | None = None
    try:
        with _temp_omnigent_mock_config(
            mock_llm_server_url, "claude", workflow_owned=bool(_server_state.get("workflow_owned"))
        ):
            created = create_native_session(
                httpx,
                live_server,
                harness="claude",
                metadata={
                    "workspace": str(workspace),
                    "terminal_launch_args": ["--permission-mode", "acceptEdits"],
                },
            )
            session_id = str(created["session_id"])
            yield (live_server, session_id, runner_id)
    finally:
        try:
            if session_id is not None:
                with contextlib.suppress(httpx.HTTPError):
                    httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            terminate_process(respawned)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_changes_tab_recovers_after_runner_binds(
    request: pytest.FixtureRequest,
    unbound_native_claude_session: tuple[str, str, str],
    mock_llm_server_url: str,
) -> None:
    """A 409 from the pre-bind window does not stick on the Changes tab."""
    base_url, session_id, runner_id = unbound_native_claude_session
    environment_url = f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    reset_mock_llm(mock_llm_server_url)
    check_token = f"check-{uuid.uuid4().hex[:6]}"
    # Claude Code's title request repeats the prompt without tools; keep it off this queue.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "PR 1 has no merge conflicts."}],
        match=check_token,
        required_tools=["Write"],
    )

    page: Page = request.getfixturevalue("page")
    with page.expect_response(lambda r: r.url == environment_url, timeout=60_000) as first:
        page.goto(f"{base_url}/c/{session_id}")
    assert first.value.status == 409, "the session was expected to open before its runner bound"
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    changes_tab = rail.get_by_role("tab", name=re.compile("^Changes"))
    changes_tab.click()
    expect(changes_tab).to_have_attribute("aria-selected", "true")
    load_error = rail.get_by_text(re.compile(r"^Failed to load:"))
    expect(load_error).to_be_visible(timeout=15_000)

    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    # No file event follows the bind, so only the bind itself can clear the error.
    expect(load_error).to_have_count(0, timeout=_RECOVERY_WINDOW_MS)
    no_changes = rail.get_by_text("No workspace changes yet")
    expect(no_changes).to_be_visible(timeout=10_000)

    # Stay in Chat view: toggling views remounts the panel and masks the stale error.
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(f"Check whether PR 1 has merge conflicts. {check_token}")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(
        page.locator(
            '[data-testid="message-bubble"][data-role="assistant"]',
            has_text="PR 1 has no merge conflicts.",
        ).last
    ).to_be_visible(timeout=120_000)
    expect(load_error).to_have_count(0)
    expect(no_changes).to_be_visible()
