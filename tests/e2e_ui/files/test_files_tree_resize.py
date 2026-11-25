"""E2E: the tabs sidebar's Files tree resizes beside a file, and shows or hides.

With ``workspace_sidebar_v2`` on, the docked tree's edge drags or steps with the arrow
keys, keeps its width across a reload, and returns to its default on a double-click.
⇧⌘E (Ctrl+Shift+E elsewhere), ⌘K's Show Files Tree / Hide Files Tree and the toolbar
toggle all run the same toggle; on a narrow page they open the tree's popover. Seeded via
the filesystem PUT endpoint (no agent run); the width rules are in the unit tests
(``useRememberedPaneWidth``).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.files._files_page import (
    is_mac,
    open_workspace,
    run_palette,
    seeded_workspace,
    set_rail_width,
)
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app

_HANDLE = "Resize the file tree"


@pytest.fixture
def readme_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    with seeded_workspace(seeded_session, {"README.md": "readme-sentinel\n"}) as session:
        yield session


def _toggle_tree_chord(page: Page) -> str:
    return "Meta+Shift+e" if is_mac(page) else "Control+Shift+e"


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
    sidebar = open_workspace(page, base_url, session_id)
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

    # A reload keeps the width.
    page.reload()
    sidebar.get_by_role("button", name="Full screen", exact=True).click()
    handle = page.get_by_role("separator", name=_HANDLE)
    expect(handle).to_have_attribute("aria-valuenow", stepped, timeout=60_000)

    # A double-click returns it to the default.
    handle.dblclick()
    expect(handle).to_have_attribute("aria-valuenow", str(default))


def _box_width(locator: Locator) -> float:
    box = locator.bounding_box()
    assert box is not None
    return box["width"]


def test_the_tree_keeps_its_share_in_full_screen(
    page: Page, readme_session: tuple[str, str]
) -> None:
    """Full screen widens the docked tree in proportion; a share chosen there stays after."""
    base_url, session_id = readme_session
    sidebar = open_workspace(page, base_url, session_id)
    page.set_viewport_size({"width": 1920, "height": 1000})
    place = _open_readme(page, sidebar, full_screen=False)
    set_rail_width(page, sidebar, 820)
    handle = page.get_by_role("separator", name=_HANDLE)
    expect(handle).to_have_count(1, timeout=10_000)
    tree = handle.locator("xpath=..")
    docked_page = _box_width(place)
    docked_share = _box_width(tree) / docked_page

    sidebar.get_by_role("button", name="Full screen", exact=True).click()
    poll_until(lambda: _box_width(place) > docked_page + 600, timeout_s=10, interval_s=0.1)
    poll_until(
        lambda: abs(_box_width(tree) / _box_width(place) - docked_share) < 0.01,
        timeout_s=10,
        interval_s=0.1,
    )

    # A wider tree dragged in full screen is kept as its share of the page.
    box = handle.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
    page.mouse.down()
    page.mouse.move(box["x"] + 200, box["y"] + 200, steps=8)
    page.mouse.up()
    chosen_share = _box_width(tree) / _box_width(place)
    assert chosen_share > docked_share + 0.05, (chosen_share, docked_share)
    page.get_by_role("button", name="Exit full screen", exact=True).click()
    poll_until(lambda: _box_width(place) < docked_page + 5, timeout_s=10, interval_s=0.1)
    poll_until(
        lambda: abs(_box_width(tree) / _box_width(place) - chosen_share) < 0.01,
        timeout_s=10,
        interval_s=0.1,
    )


def test_the_tree_shows_and_hides_from_the_chord_the_palette_and_the_toolbar(
    page: Page, readme_session: tuple[str, str]
) -> None:
    base_url, session_id = readme_session
    sidebar = open_workspace(page, base_url, session_id)
    place = _open_readme(page, sidebar, full_screen=True)
    header = place.get_by_test_id("files-page-tree-header")
    expect(header).to_be_visible(timeout=30_000)

    page.keyboard.press(_toggle_tree_chord(page))
    expect(header).to_have_count(0)
    expect(place.get_by_role("button", name="Show Files Tree")).to_be_visible()
    run_palette(page, "Show Files Tree")
    expect(header).to_be_visible()
    place.get_by_role("button", name="Hide Files Tree").click()
    expect(header).to_have_count(0)
    run_palette(page, "Show Files Tree")
    expect(header).to_be_visible()


def test_a_narrow_page_opens_the_tree_popover_from_the_chord(
    page: Page, readme_session: tuple[str, str]
) -> None:
    base_url, session_id = readme_session
    sidebar = open_workspace(page, base_url, session_id)
    _open_readme(page, sidebar, full_screen=False)
    popover = page.get_by_role("dialog", name="Workspace files")
    expect(popover).to_have_count(0)

    page.keyboard.press(_toggle_tree_chord(page))
    expect(popover).to_be_visible()
    expect(popover.get_by_test_id("files-page-tree-header")).to_be_visible()
    expect(page.get_by_role("separator", name=_HANDLE)).to_have_count(0)

    page.keyboard.press(_toggle_tree_chord(page))
    expect(popover).to_have_count(0)
    run_palette(page, "Show Files Tree")
    expect(popover).to_be_visible()
