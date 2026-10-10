"""E2E: browsing the Files rail into a subfolder should move the session's workdir.

Double-clicking a folder in the Workspace rail's Files tab re-roots the tree
onto it and the header names that folder. The session's working directory is
expected to follow: a shell opened afterwards from the rail's "+" menu should
start in the browsed folder, and the session's recorded workspace (the
composer's Working-directory chip, ``GET /v1/sessions/{id}``) should name it.

The shell's cwd is read back through the product rather than the DOM: xterm
renders to a canvas, so ``pwd`` output never reaches the DOM, and the runner's
filesystem is not shared with the test process. The shell tees ``pwd`` into a
file under the workspace root, which the filesystem API then serves.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

_FOLDER = "workdir-demo"
_PWD_PROBE = "pwd-probe.txt"


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    # Wide enough for the rail's shell pane to stay legible beside the chat.
    return {**browser_context_args, "viewport": {"width": 1440, "height": 900}}


def _filesystem_url(base_url: str, session_id: str, path: str) -> str:
    return f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}"


def _environment_root(base_url: str, session_id: str) -> str:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default", timeout=30.0
    )
    resp.raise_for_status()
    return resp.json()["metadata"]["root"]


def _seed_folder(base_url: str, session_id: str) -> None:
    resp = httpx.put(
        _filesystem_url(base_url, session_id, f"{_FOLDER}/inside.txt"),
        json={"content": "proof the tree re-rooted\n", "encoding": "utf-8"},
        timeout=30.0,
    )
    resp.raise_for_status()


def _read_workspace_file(base_url: str, session_id: str, path: str) -> str | None:
    resp = httpx.get(_filesystem_url(base_url, session_id, path), timeout=30.0)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["content"]


def _session_workspace(base_url: str, session_id: str) -> str | None:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("workspace")


def _reroot_files_rail(page: Page) -> Locator:
    """Open the Workspace rail's Files tab and double-click the seeded folder.

    :param page: Playwright page already navigated to ``/c/{id}``.
    :returns: The Workspace rail, re-rooted onto the folder.
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()

    row = rail.get_by_role("button", name=f"{_FOLDER}/", exact=True)
    expect(row).to_be_visible(timeout=30_000)
    row.dblclick()

    expect(rail.get_by_text("inside.txt")).to_be_visible(timeout=30_000)
    expect(row).to_have_count(0)
    expect(rail.get_by_text(_FOLDER, exact=True)).to_be_visible()
    return rail


def _open_new_shell(page: Page, rail: Locator) -> Locator:
    """Create a shell from the rail's "+" → Shell menu and wait for it to connect.

    :param page: Playwright page on the session.
    :param rail: The Workspace rail locator.
    :returns: The connected shell's terminal view.
    """
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
    return terminal_view


def test_new_shell_opens_in_the_folder_the_files_rail_was_rerooted_onto(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """A shell opened after re-rooting the Files rail starts in the browsed folder."""
    base_url, session_id = terminal_session
    root = _environment_root(base_url, session_id)
    _seed_folder(base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    rail = _reroot_files_rail(page)
    terminal_view = _open_new_shell(page, rail)

    terminal_view.locator("textarea.xterm-helper-textarea").focus()
    page.keyboard.type(f"pwd | tee '{root}/{_PWD_PROBE}'")
    page.keyboard.press("Enter")

    deadline = time.monotonic() + 30
    observed: str | None = None
    while time.monotonic() < deadline:
        observed = _read_workspace_file(base_url, session_id, _PWD_PROBE)
        if observed and observed.strip():
            break
        page.wait_for_timeout(500)
    assert observed and observed.strip(), "the shell never wrote its pwd probe"
    # Keep the pwd output on screen briefly so a recording ends on it.
    page.wait_for_timeout(1_500)

    expected = f"{root}/{_FOLDER}"
    assert observed.strip() == expected, (
        f"new shell started in {observed.strip()!r} after re-rooting the Files rail "
        f"onto {_FOLDER!r}; expected {expected!r}"
    )


def test_session_workdir_follows_the_files_rail_reroot(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """Re-rooting the Files rail updates the session's recorded working directory."""
    base_url, session_id = terminal_session
    root = _environment_root(base_url, session_id)
    _seed_folder(base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    chip = page.get_by_test_id("composer-workspace-dir")
    expect(chip).to_be_visible(timeout=30_000)
    _reroot_files_rail(page)

    expected = f"{root}/{_FOLDER}"
    try:
        expect(chip).to_have_attribute(
            "aria-label", f"Working directory: {expected}", timeout=10_000
        )
    except AssertionError as exc:
        raise AssertionError(
            f"composer chip still reads {chip.get_attribute('aria-label')!r} after "
            f"re-rooting the Files rail onto {_FOLDER!r}; session workspace="
            f"{_session_workspace(base_url, session_id)!r}, environment root="
            f"{_environment_root(base_url, session_id)!r}, expected {expected!r}"
        ) from exc
    assert _session_workspace(base_url, session_id) == expected
