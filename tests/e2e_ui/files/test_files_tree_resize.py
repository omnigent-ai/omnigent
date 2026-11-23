"""E2E: the tabs sidebar's Files tree resizes beside a file, and shows or hides.

With ``workspace_sidebar_v2`` on, the docked tree's edge drags or steps with the arrow
keys, keeps its width across a reload, and returns to its default on a double-click.
⇧⌘E (Ctrl+Shift+E elsewhere), ⌘K's Show Files Tree / Hide Files Tree and the toolbar
toggle all run the same toggle; on a narrow page they open the tree's popover. Seeded via
the filesystem PUT endpoint (no agent run); the width rules are in the unit tests
(``useRememberedPaneWidth``).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.files.test_file_breadcrumbs import _mac, _open_workspace
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HANDLE = "Resize the file tree"


@pytest.fixture
def readme_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/README.md",
        json={"content": "readme-sentinel\n", "encoding": "utf-8"},
        timeout=10.0,
    ).raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _toggle_tree_chord(page: Page) -> str:
    return "Meta+Shift+e" if _mac(page) else "Control+Shift+e"


def _run_palette(page: Page, label: str) -> None:
    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_test_id("command-palette-input")
    expect(palette).to_be_focused(timeout=10_000)
    palette.fill(label)
    page.get_by_role("option", name=re.compile(rf"^{re.escape(label)}")).click()
    expect(palette).to_have_count(0)


def _open_readme(page: Page, sidebar: Locator, *, full_screen: bool) -> Locator:
    """Opens README.md from the Files tab's tree and returns its page."""
    _open_app(page, sidebar, "Files")
    if full_screen:
        page.get_by_role("button", name="Full screen", exact=True).click()
    sidebar.get_by_role("treeitem", name="README.md", exact=True).click(timeout=30_000)
    place = sidebar.locator('[data-testid="workspace-files-place"]:visible')
    expect(place.get_by_text("readme-sentinel")).to_be_visible(timeout=30_000)
    return place


def _width(handle: Locator) -> int:
    return int(handle.get_attribute("aria-valuenow") or 0)


def test_the_docked_tree_resizes_and_keeps_its_width(
    page: Page, readme_session: tuple[str, str]
) -> None:
    base_url, session_id = readme_session
    sidebar = _open_workspace(page, base_url, session_id)
    _open_readme(page, sidebar, full_screen=True)
    handle = page.get_by_role("separator", name=_HANDLE)
    expect(handle).to_be_visible(timeout=30_000)
    default = _width(handle)
    assert default > 0

    # Drag the edge 80px narrower; the width saves once the drag ends.
    box = handle.bounding_box()
    assert box is not None
    start_x, middle_y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(start_x, middle_y)
    page.mouse.down()
    page.mouse.move(start_x - 40, middle_y, steps=4)
    page.mouse.move(start_x - 80, middle_y, steps=4)
    page.mouse.up()
    expect(handle).not_to_have_attribute("aria-valuenow", str(default))
    dragged = _width(handle)
    assert abs(dragged - (default - 80)) <= 5, (dragged, default)

    # The arrow keys step it from the keyboard.
    handle.focus()
    page.keyboard.press("ArrowLeft")
    stepped = str(dragged - 16)
    expect(handle).to_have_attribute("aria-valuenow", stepped)

    # A reload keeps the width once the page is full screen again.
    page.reload()
    page.get_by_role("button", name="Full screen", exact=True).click(timeout=60_000)
    handle = page.get_by_role("separator", name=_HANDLE)
    expect(handle).to_have_attribute("aria-valuenow", stepped, timeout=60_000)

    # A double-click returns it to the default.
    handle.dblclick()
    expect(handle).to_have_attribute("aria-valuenow", str(default))


def test_the_tree_shows_and_hides_from_the_chord_the_palette_and_the_toolbar(
    page: Page, readme_session: tuple[str, str]
) -> None:
    base_url, session_id = readme_session
    sidebar = _open_workspace(page, base_url, session_id)
    place = _open_readme(page, sidebar, full_screen=True)
    header = place.get_by_test_id("files-page-tree-header")
    expect(header).to_be_visible(timeout=30_000)

    page.keyboard.press(_toggle_tree_chord(page))
    expect(header).to_have_count(0)
    expect(place.get_by_role("button", name="Show Files Tree")).to_be_visible()
    _run_palette(page, "Show Files Tree")
    expect(header).to_be_visible()
    place.get_by_role("button", name="Hide Files Tree").click()
    expect(header).to_have_count(0)
    _run_palette(page, "Show Files Tree")
    expect(header).to_be_visible()


def test_a_narrow_page_opens_the_tree_popover_from_the_chord(
    page: Page, readme_session: tuple[str, str]
) -> None:
    base_url, session_id = readme_session
    sidebar = _open_workspace(page, base_url, session_id)
    _open_readme(page, sidebar, full_screen=False)
    popover = page.get_by_role("dialog", name="Workspace files")
    expect(popover).to_have_count(0)

    page.keyboard.press(_toggle_tree_chord(page))
    expect(popover).to_be_visible()
    expect(popover.get_by_test_id("files-page-tree-header")).to_be_visible()
    expect(page.get_by_role("separator", name=_HANDLE)).to_have_count(0)

    page.keyboard.press(_toggle_tree_chord(page))
    expect(popover).to_have_count(0)
    _run_palette(page, "Show Files Tree")
    expect(popover).to_be_visible()
