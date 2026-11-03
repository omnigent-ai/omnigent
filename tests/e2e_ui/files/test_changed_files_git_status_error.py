"""E2E: a git-status failure surfaces in the Files panel, not a blank list.

Regression guard for the "blank panel == clean tree" ambiguity. The changed-files
view (``/changes`` -> ``GitFilesystemRegistry.list_changed_files``) used to swallow
every ``git status`` failure to an empty list, which ``FlatFileList`` renders
identically to a genuinely clean tree ("No workspace changes yet"). A read that
*could not run* was therefore indistinguishable from "nothing changed", which is
exactly what made a real worktree report impossible to diagnose.

On failure the runner answers ``500 {error:{code:"git_status_failed", message}}``, which
the server's runner proxy relays to the browser as ``502 {detail: message}``. The web
hook (``useWorkspaceChangedFiles``) parses that body and throws so the panel shows
"Failed to load: <reason>" (in the destructive style) instead of the misleading
empty state.

The test breaks the session repository's index, so a real git failure travels
through the live runner and server proxy without interception.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e_ui.conftest import (
    _build_hello_world_bundle,
    _ensure_runner_online,
    _server_state,
    open_right_rail,
)


@pytest.fixture
def git_repository_session(
    live_server: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A runner-bound session whose workspace is a git repo with one modified file."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("VALUE = 0\n")
    git = ["git", "-C", str(repo), "-c", "user.email=e2e@example.com", "-c", "user.name=e2e"]
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-qm", "init"]):
        subprocess.run([*git, *args], check=True, capture_output=True, timeout=30)
    (repo / "src" / "app.py").write_text("VALUE = 1\n")

    create = post_session_bundle(
        httpx.post,
        f"{live_server}/v1/sessions",
        _build_hello_world_bundle(),
        metadata={"workspace": str(repo)},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]

    respawned: subprocess.Popen[bytes] | None = None
    try:
        respawned = _ensure_runner_online(live_server, tmp_path_factory)
        bind_session_runner(
            httpx.patch, live_server, session_id, str(_server_state["runner_id"]), timeout=10.0
        )
        yield (live_server, session_id, repo)
    finally:
        try:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def test_real_git_status_failure_reason_reaches_changes_pane(
    page: Page,
    git_repository_session: tuple[str, str, Path],
) -> None:
    """A live runner's git-status failure shows its reason, not the proxy's status line."""
    base_url, session_id, repo = git_repository_session
    (repo / ".git" / "index").write_bytes(b"not a git index")

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    changes_tab = rail.get_by_role("tab", name=re.compile("^Changes"))
    changes_tab.click()
    expect(changes_tab).to_have_attribute("aria-selected", "true")

    error_line = rail.get_by_text(re.compile(r"^Failed to load:"))
    expect(error_line).to_be_visible(timeout=30_000)
    expect(error_line).to_contain_text("git status exited 128")
    expect(rail.get_by_text("No workspace changes yet")).to_have_count(0)
