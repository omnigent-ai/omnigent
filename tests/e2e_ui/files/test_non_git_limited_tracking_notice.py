"""E2E: a non-git workspace's Changes tab explains its limited tracking.

In a workspace that is not a git repository, only edits made through the
agent's file tools are recorded; files written by a native-harness CLI, shell
commands, or any external process never reach the changes list. The panel used
to render the bare empty state ("No workspace changes yet") there, which reads
as a definitive "nothing changed" even when files did change on disk.

This drives the real user journey end to end — a runner-bound session whose
stored workspace is a plain (non-git) folder where a file was written straight
to disk (what a native-harness CLI or external process does), the session page
opened in the SPA, the Workspace rail's Changes tab selected — with no request
interception: the SPA hits the live server, which proxies to the live runner,
which builds the filesystem registry for the real workspace path. The
assertion encodes the CORRECT behavior: the panel surfaces the "Limited change
tracking" notice explaining why the list may be partial. On an unfixed build
the silent empty state renders instead and this test fails there — the
regression guard for the fix.
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
    """A runner-bound session whose workspace is a plain non-git folder.

    A file is written straight to disk — the shape of a native-harness CLI or
    external-process edit, which never flows through ``record_change`` and so
    can never appear in the changes list.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path: Per-test dir for the non-git workspace (outside any repo).
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    workspace = tmp_path / "scratch" / "project"
    workspace.mkdir(parents=True)
    (workspace / "edited-by-cli.txt").write_text("written outside the agent's file tools\n")

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


def test_non_git_changes_tab_shows_limited_tracking_notice(
    page: Page,
    non_git_workspace_session: tuple[str, str],
) -> None:
    """The Changes tab explains limited tracking instead of a silent empty list."""
    base_url, session_id = non_git_workspace_session

    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # The changed-files list — where the silent empty state rendered — is the
    # Changes rail tab (a peer of Files). Select it explicitly so the
    # assertion does not depend on the remembered tab from a prior session.
    changes_tab = rail.get_by_role("tab", name=re.compile("^Changes"))
    changes_tab.click()
    expect(changes_tab).to_have_attribute("aria-selected", "true")

    # The workspace is not a git repo and the on-disk edit bypassed the
    # agent's file tools, so the list cannot include it. The panel must say
    # WHY tracking is limited rather than render the bare empty state that
    # reads as "nothing changed".
    expect(rail.get_by_text("Limited change tracking")).to_be_visible(timeout=30_000)
    expect(rail.get_by_text(re.compile("isn't a Git repository"))).to_be_visible()

    # The silent empty state — the buggy rendering — must not appear.
    expect(rail.get_by_text("No workspace changes yet")).to_have_count(0)

    # And this degraded state is an explanation, not a failure.
    expect(rail.get_by_text(re.compile(r"^Failed to load:"))).to_have_count(0)
