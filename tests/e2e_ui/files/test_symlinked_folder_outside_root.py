"""E2E: the Files sidebar expands a symlinked folder whose target is outside the workspace.

A project directory can hold symlinks to directories elsewhere on the host, such as git
worktrees under ``~/.worktrees``. The All tree lists such an entry as a folder, so
expanding it must list the linked directory's files rather than render the
"Failed to load this folder." placeholder, and a listed file must open in the viewer.
A regular subdirectory and a symlink whose target is inside the workspace are expanded
first as controls.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, Response, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, open_right_rail

_OUTSIDE_FILE = "inside-outside.txt"
_OUTSIDE_CONTENT = "file inside the linked worktree"
_REGULAR_FILE = "inside-regular.txt"
_LINKED_OUTSIDE = "linked-worktree"
_LINKED_INSIDE = "linked-inside"
_REGULAR_DIR = "regular-dir"

_PROJECT_AGENT_YAML = """\
name: symlinked_project
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: {workspace}
  sandbox:
    type: none
"""


@pytest.fixture
def symlinked_workspace_session(
    live_server: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound session whose workspace symlinks to a directory outside its root."""
    outside = tmp_path / "worktrees" / "feature-branch"
    outside.mkdir(parents=True)
    (outside / _OUTSIDE_FILE).write_text(f"{_OUTSIDE_CONTENT}\n")

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "README.md").write_text("project root\n")
    (workspace / _REGULAR_DIR).mkdir()
    (workspace / _REGULAR_DIR / _REGULAR_FILE).write_text("regular dir file\n")
    (workspace / _LINKED_INSIDE).symlink_to(_REGULAR_DIR, target_is_directory=True)
    (workspace / _LINKED_OUTSIDE).symlink_to(outside, target_is_directory=True)

    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    yaml_text = _PROJECT_AGENT_YAML.format(workspace=workspace)
    bundle = bundle_files({"symlinked_project.yaml": yaml_text.encode()})
    create = post_session_bundle(httpx.post, f"{live_server}/v1/sessions", bundle, timeout=30.0)
    create.raise_for_status()
    session_id = create.json()["session_id"]
    bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


def _expand(rail: Locator, name: str) -> Locator:
    row = rail.get_by_role("button", name=f"{name}/", exact=True)
    expect(row).to_be_visible(timeout=15_000)
    row.click()
    expect(row).to_have_attribute("aria-expanded", "true")
    return row


def test_symlinked_folder_outside_workspace_lists_its_contents(
    request: pytest.FixtureRequest,
    symlinked_workspace_session: tuple[str, str],
) -> None:
    """A symlink to a directory outside the workspace lists its files, which then open."""
    base_url, session_id = symlinked_workspace_session
    listing_statuses: list[int] = []

    def _track_listing(response: Response) -> None:
        if f"/filesystem/{_LINKED_OUTSIDE}" in response.url:
            listing_statuses.append(response.status)

    page: Page = request.getfixturevalue("page")
    page.on("response", _track_listing)
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    expect(rail.get_by_role("searchbox", name="Search all files")).to_be_visible(timeout=30_000)

    _expand(rail, _REGULAR_DIR)
    expect(rail.get_by_text(_REGULAR_FILE)).to_have_count(1, timeout=15_000)
    _expand(rail, _LINKED_INSIDE)
    expect(rail.get_by_text(_REGULAR_FILE)).to_have_count(2, timeout=15_000)

    _expand(rail, _LINKED_OUTSIDE)
    outside_file = rail.get_by_text(_OUTSIDE_FILE)
    failed = rail.get_by_text("Failed to load this folder.")
    expect(outside_file.or_(failed)).to_be_visible(timeout=30_000)
    expect(
        failed,
        f"linked folder listing requests returned {listing_statuses}",
    ).to_have_count(0, timeout=3_000)
    expect(outside_file).to_be_visible()

    # The row's open button carries the name as visible text; the icon-only
    # copy-path button beside it carries it only in its label.
    rail.get_by_role("button", name=re.compile(re.escape(_OUTSIDE_FILE))).filter(
        has_text=_OUTSIDE_FILE
    ).click()
    file_viewer = rail.get_by_test_id("file-viewer")
    expect(file_viewer).to_be_visible()
    expect(file_viewer.get_by_text(_OUTSIDE_CONTENT).first).to_be_visible(timeout=20_000)
