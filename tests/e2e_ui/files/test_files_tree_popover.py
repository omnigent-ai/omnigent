"""E2E: on a narrow page, the tabs sidebar's Files tree opens in its popover.

With ``workspace_sidebar_v2`` on, ⇧⌘E (Ctrl+Shift+E elsewhere) and ⌘K's Show Files Tree
open the tree's popover beside a file, with the tree's header and no resize edge. The
docked tree's commands and width rules are in the unit tests (``WorkspacePlaceView``,
``useRememberedPaneWidth``). Seeded via the filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.files._files_page import is_mac, open_workspace, run_palette, seeded_workspace
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app

_HANDLE = "Resize the file tree"


@pytest.fixture
def readme_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    with seeded_workspace(seeded_session, {"README.md": "readme-sentinel\n"}) as session:
        yield session


def _toggle_tree_chord(page: Page) -> str:
    return "Meta+Shift+e" if is_mac(page) else "Control+Shift+e"


def _open_readme(page: Page, sidebar: Locator) -> Locator:
    """Opens README.md from the Files tab's tree and returns its page."""
    _open_app(page, sidebar, "Files")
    sidebar.get_by_role("treeitem", name="README.md", exact=True).click(timeout=30_000)
    place = sidebar.locator('[data-testid="workspace-files-place"]:visible')
    expect(place.get_by_text("readme-sentinel")).to_be_visible(timeout=30_000)
    return place


def test_a_narrow_page_opens_the_tree_popover_from_the_chord(
    page: Page, readme_session: tuple[str, str]
) -> None:
    base_url, session_id = readme_session
    sidebar = open_workspace(page, base_url, session_id)
    _open_readme(page, sidebar)
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
