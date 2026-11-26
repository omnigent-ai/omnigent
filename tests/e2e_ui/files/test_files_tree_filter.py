"""E2E: the tabs sidebar's Files tree filters by name, narrowed by globs.

With ``workspace_sidebar_v2`` on, the tree header's Filter Files Tree by Name opens a
filter under it. Typing a name shows the workspace's matches as a tree, under a strip
that says what the filter does. Include and exclude globs narrow the search, a match
opens like a tree row, and Clear or Escape return to the whole tree. ⌘K's row opens the
filter with focus in it, and closing it, in the narrow popover too, leaves focus on its
button. Seeded via the filesystem PUT endpoint (no agent run); the
filter's states are in the unit tests (``FilesPageTree``, ``useTreeFilters``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.files._files_page import open_workspace, run_palette, seeded_workspace
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app

_FILES = {
    "src/ui/button.ts": "button-sentinel\n",
    "src/ui/button.test.ts": "button test\n",
    "docs/button.md": "# Button\n",
    "README.md": "# Repo\n",
}


@pytest.fixture
def filter_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    with seeded_workspace(seeded_session, _FILES) as session:
        yield session


def _row(container: Locator, path: str) -> Locator:
    return container.locator(f'[role="treeitem"][data-item-path="{path}"]')


def test_the_tree_filters_by_name_narrows_by_globs_and_clears(
    page: Page, filter_session: tuple[str, str]
) -> None:
    base_url, session_id = filter_session
    sidebar = open_workspace(page, base_url, session_id)
    _open_app(page, sidebar, "Files")
    expect(sidebar.get_by_role("treeitem", name="README.md", exact=True)).to_be_visible(
        timeout=30_000
    )

    sidebar.get_by_role("button", name="Filter Files Tree by Name").click()
    name = sidebar.get_by_role("textbox", name="Name contains")
    expect(name).to_be_focused()
    name.fill("button")
    strip = sidebar.get_by_test_id("tree-filter-strip")
    expect(strip).to_have_text(re.compile("Names containing “button”"), timeout=15_000)
    matches = sidebar.get_by_role("tree", name="Matching workspace files")
    for path in ("src/ui/button.ts", "src/ui/button.test.ts", "docs/button.md"):
        expect(_row(matches, path)).to_be_visible(timeout=15_000)
    expect(_row(matches, "README.md")).to_have_count(0)

    # A glob narrows the matches to the files it includes.
    sidebar.get_by_role("button", name="Files to include or exclude").click()
    sidebar.get_by_role("textbox", name="Files to exclude").fill("*.test.ts, docs/**")
    expect(strip).to_contain_text("not *.test.ts, docs/**", timeout=15_000)
    expect(_row(matches, "src/ui/button.test.ts")).to_have_count(0, timeout=15_000)
    expect(_row(matches, "docs/button.md")).to_have_count(0)

    # A match opens like a tree row, and the filter stays on.
    _row(matches, "src/ui/button.ts").click()
    expect(sidebar.get_by_role("tab", name=re.compile(r"^button\.ts"))).to_have_attribute(
        "aria-selected", "true", timeout=15_000
    )
    place = sidebar.locator('[data-testid="workspace-files-place"]:visible')
    expect(place.get_by_text("button-sentinel")).to_be_visible(timeout=30_000)

    # Back returns to the filtered tree, and Clear to the whole tree.
    sidebar.get_by_role("group", name="Workspace history").get_by_role(
        "button", name="Back"
    ).click()
    strip = sidebar.get_by_test_id("tree-filter-strip")
    expect(strip).to_be_visible()
    strip.get_by_role("button", name="Clear").click()
    expect(sidebar.get_by_role("treeitem", name="README.md", exact=True)).to_be_visible()
    expect(strip).to_have_count(0)

    # ⌘K opens the filter with focus in it; Escape empties the name, then closes the filter.
    sidebar.get_by_role("button", name="Filter Files Tree by Name").click()
    expect(name).to_have_count(0)
    run_palette(page, "Filter Files Tree by Name")
    expect(name).to_be_focused(timeout=10_000)
    name.fill("readme")
    expect(
        _row(sidebar.get_by_role("tree", name="Matching workspace files"), "README.md")
    ).to_be_visible(timeout=15_000)
    name.press("Escape")
    expect(name).to_have_value("")
    name.press("Escape")
    expect(name).to_have_count(0)
    # Focus moves to the filter's button rather than dropping to the page.
    expect(sidebar.get_by_role("button", name="Filter Files Tree by Name")).to_be_focused()


def test_closing_the_filter_in_the_narrow_popover_keeps_focus_in_it(
    page: Page, filter_session: tuple[str, str]
) -> None:
    base_url, session_id = filter_session
    sidebar = open_workspace(page, base_url, session_id)
    _open_app(page, sidebar, "Files")
    sidebar.get_by_role("treeitem", name="README.md", exact=True).click(timeout=30_000)
    # With a file open, the narrow page's tree is a popover.
    sidebar.get_by_role("button", name="Show Files Tree").click()
    popover = page.get_by_role("dialog", name="Workspace files")
    expect(popover).to_be_visible()

    toggle = popover.get_by_role("button", name="Filter Files Tree by Name")
    toggle.click()
    name = popover.get_by_role("textbox", name="Name contains")
    expect(name).to_be_focused()
    name.press("Escape")
    expect(name).to_have_count(0)
    expect(popover).to_be_visible()
    expect(toggle).to_be_focused()

    toggle.click()
    popover.get_by_role("button", name="Close filter").click()
    expect(popover).to_be_visible()
    expect(toggle).to_be_focused()
