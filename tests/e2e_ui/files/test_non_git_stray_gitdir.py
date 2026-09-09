"""E2E: a session in a non-git folder below a stray ``.git`` dir shows no 502.

A workspace that is not inside a git repository can still sit below a
directory literally named ``.git`` that is NOT a repository — e.g. a leftover
``~/.git`` holding only omnigent's untracked-cache lock file. ``_find_git_root``
treated any ancestor ``.git`` entry as a repository, so such a workspace got a
``GitFilesystemRegistry`` rooted at a non-repo: every ``git status`` exited 128,
the runner's ``/changes`` endpoint answered 500 ``git_status_failed``, the
server proxy wrapped that as a 502, and the Files panel rendered
"Failed to load: 502" instead of the changed-files list.

This drives the real user journey end to end — a runner-bound session whose
stored workspace is a plain (non-git) folder below a stray non-repo ``.git``
directory, the session page opened in the SPA, the Workspace rail's Changes
tab selected — with no request interception: the SPA hits the live server,
which proxies to the live runner, which builds the filesystem registry for the
real workspace path. The assertion encodes the CORRECT behavior: a non-git
workspace has no git status to fail, so the panel must show the normal empty
state ("No workspace changes yet") and never an error line. On a build with
the stray-``.git`` bug the panel shows "Failed to load: 502 …" and this test
fails there — the regression guard for the fix.
"""

from __future__ import annotations

import json
import re
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
    open_right_rail,
)


@pytest.fixture
def non_git_workspace_session(
    live_server: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound session whose workspace is non-git below a stray ``.git``.

    Recreates the reporter-machine shape: a ``.git`` DIRECTORY that is not a
    repository (it holds only omnigent's untracked-cache lock file) with a
    plain non-git workspace nested below it. The session pins that workspace
    via ``metadata.workspace``, which is what the runner's per-session
    filesystem registry resolves against.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path: Per-test dir for the stray-``.git`` tree (outside any repo).
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    stray_home = tmp_path / "home"
    (stray_home / ".git").mkdir(parents=True)
    (stray_home / ".git" / "omnigent-untracked-cache.lock").touch()
    workspace = stray_home / "scratch" / "project"
    workspace.mkdir(parents=True)
    (workspace / "notes.txt").write_text("hello from a non-git folder\n")

    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    bundle = _build_hello_world_bundle()
    create = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(workspace)})},
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


def test_non_git_workspace_under_stray_gitdir_shows_empty_changes(
    page: Page,
    non_git_workspace_session: tuple[str, str],
) -> None:
    """The Changes tab shows the empty state for a non-git workspace, never a 502."""
    base_url, session_id = non_git_workspace_session

    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # The changed-files list — where the failure rendered — is the Changes
    # rail tab (a peer of Files). Select it explicitly so the assertion does
    # not depend on the remembered tab from a prior session.
    changes_tab = rail.get_by_role("tab", name=re.compile("^Changes"))
    changes_tab.click()
    expect(changes_tab).to_have_attribute("aria-selected", "true")

    # A non-git workspace has no git status to fail: the panel must settle to
    # the normal empty state. A build that misroots the workspace on the
    # stray ``.git`` shows "Failed to load: 502 ..." here instead, so this
    # assertion is what fails on the broken path.
    expect(rail.get_by_text("No workspace changes yet")).to_be_visible(timeout=30_000)

    # And the failure line must never render for a non-git workspace.
    expect(rail.get_by_text(re.compile(r"^Failed to load:"))).to_have_count(0)
