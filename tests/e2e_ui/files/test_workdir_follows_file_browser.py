"""E2E: re-rooting the Files panel's working folder must move the session workdir.

The Files rail header labels the browsed location "Working folder", and
double-clicking a folder re-roots the panel there. But the browse location is
panel-local state: nothing tells the server or runner, so the session's actual
working directory — where agent turns run and where new shells open (the
session snapshot's ``workspace``, which the runner cd's into) — never follows.
A user who navigates the file browser into a subfolder and then opens a shell
(or sends a turn) still lands in the original root, even though the panel
claims the subfolder is the working folder.

The journey pinned here:

1. open a session whose working folder is the workspace root
2. open the Workspace rail → Files tab and double-click a subfolder — the
   panel re-roots and the header names the subfolder as the working folder
3. open a new shell from the rail's "+" menu and type ``pwd`` — today the
   shell opens in the ORIGINAL root (the user-visible contrast; xterm renders
   to a canvas, so the printed path is left to the recording rather than a
   DOM assertion)
4. the session's workdir must now be the subfolder — asserted server-side:
   the session snapshot's ``workspace`` or the default environment root
   reflects the browsed folder. Today neither ever changes, which is the bug.
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_FOLDER = "workdir-demo"


def _session_workdirs(
    page: Page, base_url: str, session_id: str
) -> tuple[str | None, str | None]:
    """Sample both server-side readouts of the session's working directory.

    :param page: Playwright page (its request context reuses the app origin).
    :param base_url: Live server base URL.
    :param session_id: Session under test.
    :returns: ``(snapshot_workspace, default_environment_root)`` — either may
        be ``None`` when unset or the endpoint is momentarily unavailable.
    """
    workspace: str | None = None
    env_root: str | None = None
    snap = page.request.get(f"{base_url}/v1/sessions/{session_id}")
    if snap.status == 200:
        value = snap.json().get("workspace")
        workspace = value if isinstance(value, str) else None
    env = page.request.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    )
    if env.status == 200:
        metadata = env.json().get("metadata") or {}
        value = metadata.get("root")
        env_root = value if isinstance(value, str) else None
    return workspace, env_root


def test_browsing_to_a_folder_changes_the_session_workdir(
    page: Page,
    terminal_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Double-clicking a folder in the Files panel moves the session workdir.

    Drives the real re-root journey (the same double-click contract pinned in
    ``test_files_panel_header.py``), then requires the session's working
    directory to follow: the session snapshot's ``workspace`` (where the
    runner cd's for turns and new shells) or the default environment root
    must resolve to the browsed folder. The panel currently keeps the browse
    location as client-only state, so neither readout ever changes and a
    shell opened after the re-root still starts in the original root.
    """
    base_url, session_id = terminal_session

    env = page.request.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    )
    assert env.status == 200, env.text()
    root = Path(env.json()["metadata"]["root"])
    folder = root / _FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "inside.txt").write_text("proof the tree re-rooted\n")
    # The workspace is a real checkout shared with every other test in the
    # shard, so this fixture directory must not outlive the test.
    request.addfinalizer(lambda: shutil.rmtree(folder, ignore_errors=True))

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()

    row = rail.get_by_role("button", name=f"{_FOLDER}/", exact=True)
    expect(row).to_be_visible(timeout=30_000)
    row.dblclick()

    # Re-rooted: the folder's own contents are the top level now, the folder
    # row is gone, and the header names the subfolder as the working folder.
    expect(rail.get_by_text("inside.txt")).to_be_visible(timeout=30_000)
    expect(row).to_have_count(0)
    expect(rail.get_by_text(_FOLDER)).to_be_visible()

    # The user-visible readout: a shell opened AFTER the re-root starts in
    # the session's workdir. Type ``pwd`` so the recording shows where it
    # landed; xterm renders to a canvas, so the tight assertion stays
    # server-side below.
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    page.keyboard.type("pwd")
    page.keyboard.press("Enter")
    # Let the shell echo the cwd before sampling server state.
    page.wait_for_timeout(1_500)

    # The session workdir must follow the browse. Poll briefly: a compliant
    # implementation may propagate the change asynchronously.
    deadline = time.monotonic() + 10
    workspace: str | None = None
    env_root: str | None = None
    while time.monotonic() < deadline:
        workspace, env_root = _session_workdirs(page, base_url, session_id)
        for candidate in (workspace, env_root):
            if candidate and Path(candidate).resolve() == folder.resolve():
                return
        page.wait_for_timeout(500)

    pytest.fail(
        f"file browser re-rooted the working folder to {folder} but the "
        f"session workdir never followed: snapshot workspace={workspace!r}, "
        f"default environment root={env_root!r}"
    )
