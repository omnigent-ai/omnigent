"""E2E: the tabs sidebar's Files tree roots at a folder, and returns to the workspace.

With ``workspace_sidebar_v2`` on, right-clicking a folder in the open file's breadcrumbs
offers Open folder as root beside Copy path. The tree then lists only that folder, its
header names it, and the header's Back to working folder returns it. ⌘K roots the tree at
the viewed file's folder and returns it too, and on a narrow page the rooted tree opens
as a popover with focus in the tree, where it stays after Back. Seeded via the
filesystem PUT endpoint (no agent run); which folders can be roots is in the unit tests
(``treeRoot``, ``FileBreadcrumbs``).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.files.test_file_breadcrumbs import _open_from_tree, _open_workspace
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FOLDER = "libs/widgets"
_FILES = {
    f"{_FOLDER}/OWNERS": "owner-sentinel\n",
    f"{_FOLDER}/notes.md": "# Notes\n",
    "libs/other/BUILD": "",
    "README.md": "# Repo\n",
}


@pytest.fixture
def nested_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    for path, content in _FILES.items():
        httpx.put(
            f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
            json={"content": content, "encoding": "utf-8"},
            timeout=10.0,
        ).raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _row(container: Locator, name: str) -> Locator:
    return container.get_by_role("treeitem", name=name, exact=True)


def _run_palette(page: Page, label: str) -> None:
    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_test_id("command-palette-input")
    expect(palette).to_be_focused(timeout=10_000)
    palette.fill(label)
    # Exactly one page offers the command, so the palette lists it once.
    option = page.get_by_role("option", name=re.compile(rf"^{re.escape(label)}"))
    expect(option).to_have_count(1)
    option.click()
    expect(palette).to_have_count(0)


def _open_owners(page: Page, sidebar: Locator) -> Locator:
    """Opens ``OWNERS`` from the Files tree and returns its viewer."""
    _open_from_tree(sidebar, f"{_FOLDER}/OWNERS")
    viewer = sidebar.locator('[data-testid="workspace-files-place"]:visible')
    expect(viewer.get_by_text("owner-sentinel")).to_be_visible(timeout=30_000)
    return viewer


def test_a_breadcrumb_folder_becomes_the_tree_root_and_the_tree_returns(
    page: Page, nested_session: tuple[str, str]
) -> None:
    base_url, session_id = nested_session
    sidebar = _open_workspace(page, base_url, session_id)
    _open_app(page, sidebar, "Files")
    # A full-screen workspace docks the tree beside the file.
    page.get_by_role("button", name="Full screen", exact=True).click()
    expect(_row(sidebar, "README.md")).to_be_visible(timeout=30_000)
    place = _open_owners(page, sidebar)
    header = place.get_by_test_id("files-page-tree-header")
    expect(header).to_be_visible(timeout=30_000)
    expect(header.get_by_role("button", name="Back to working folder")).to_have_count(0)

    # The folder's menu offers the root beside Copy path; the file's has no root.
    breadcrumbs = place.get_by_role("navigation", name="File path")
    breadcrumbs.get_by_role("button", name="OWNERS", exact=True).click(button="right")
    expect(page.get_by_role("menuitem", name="Copy path")).to_be_visible()
    expect(page.get_by_role("menuitem", name="Open folder as root")).to_have_count(0)
    page.keyboard.press("Escape")
    breadcrumbs.get_by_role("button", name="widgets", exact=True).click(button="right")
    expect(page.get_by_role("menuitem", name="Copy path")).to_be_visible()
    page.get_by_role("menuitem", name="Open folder as root").click()

    # The tree lists only that folder, and its header names it.
    expect(_row(place, "notes.md")).to_be_visible(timeout=30_000)
    expect(_row(place, "OWNERS")).to_have_attribute("aria-level", "1")
    expect(_row(place, "README.md")).to_have_count(0)
    expect(header).to_contain_text("widgets")

    # Opening a file from the rooted tree opens it by its workspace path.
    _row(place, "notes.md").click()
    expect(sidebar.get_by_role("tab", name=re.compile(r"^notes\.md"))).to_have_attribute(
        "aria-selected", "true", timeout=15_000
    )
    place = sidebar.locator('[data-testid="workspace-files-place"]:visible')
    expect(place.get_by_role("navigation", name="File path")).to_contain_text("widgets")
    header = place.get_by_test_id("files-page-tree-header")

    # The header's way back returns the tree to the workspace.
    header.get_by_role("button", name="Back to working folder").click()
    expect(_row(place, "README.md")).to_be_visible(timeout=30_000)
    expect(header.get_by_role("button", name="Back to working folder")).to_have_count(0)

    # ⌘K roots the tree at the viewed file's folder, and returns it.
    _run_palette(page, "Show Active File’s Folder as Tree Root")
    expect(_row(place, "OWNERS")).to_be_visible(timeout=30_000)
    expect(_row(place, "README.md")).to_have_count(0)
    _run_palette(page, "Back to working folder")
    expect(_row(place, "README.md")).to_be_visible(timeout=30_000)


def test_a_breadcrumb_root_opens_the_hidden_tree_on_a_narrow_page(
    page: Page, nested_session: tuple[str, str]
) -> None:
    base_url, session_id = nested_session
    sidebar = _open_workspace(page, base_url, session_id)
    _open_app(page, sidebar, "Files")
    expect(_row(sidebar, "README.md")).to_be_visible(timeout=30_000)
    place = _open_owners(page, sidebar)
    # The narrow page's tree is a popover, closed while the file shows.
    expect(page.get_by_role("dialog", name="Workspace files")).to_have_count(0)

    breadcrumbs = place.get_by_role("navigation", name="File path")
    breadcrumbs.get_by_role("button", name="widgets", exact=True).click(button="right")
    page.get_by_role("menuitem", name="Open folder as root").click()
    tree = page.get_by_role("dialog", name="Workspace files")
    expect(_row(tree, "notes.md")).to_be_visible(timeout=30_000)
    expect(_row(tree, "README.md")).to_have_count(0)
    # The popover starts in the rooted tree, so the keyboard can move through it at once.
    focused_row = tree.locator('[role="treeitem"]:focus')
    expect(focused_row).to_have_count(1, timeout=10_000)

    # Back to working folder leaves focus in the tree it returns to.
    tree.get_by_role("button", name="Back to working folder").click()
    expect(_row(tree, "README.md")).to_be_visible(timeout=30_000)
    expect(focused_row).to_have_count(1, timeout=10_000)
