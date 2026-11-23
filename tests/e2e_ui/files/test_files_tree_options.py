"""E2E: the tabs sidebar's Files tree sorts and shows hidden files from its menu and ⌘K.

With ``workspace_sidebar_v2`` on, the Files page's "Files tree options" menu and ⌘K's
"Sort Files Tree by …" and "Show Hidden Files" / "Hide Hidden Files" rows run the same
actions. Hidden files show until they're hidden. Seeded via the filesystem PUT endpoint
(no agent run); the menu and the rows' availability are in the unit tests
(``WorkspacePlaceView``).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.files._files_page import open_workspace, run_palette, seeded_workspace
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app

# Alphabetical and size order disagree, and one file is hidden (shown by default). The
# large file is written first, so only a real size sort puts it before the newer alpha.txt.
_FILES = {"zulu.txt": "z" * 4000 + "\n", "alpha.txt": "a\n", ".env": "SECRET=1\n"}


@pytest.fixture
def options_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    with seeded_workspace(seeded_session, _FILES) as session:
        yield session


def _names(sidebar: Locator) -> list[str]:
    """The rows' paths in order; a row's text is split where its name truncates."""
    return sidebar.locator('[role="treeitem"]').evaluate_all(
        "rows => rows.map(row => row.dataset.itemPath)"
    )


def _expect_order(sidebar: Locator, first: str, second: str) -> None:
    def ordered() -> bool:
        names = _names(sidebar)
        return first in names and second in names and names.index(first) < names.index(second)

    poll_until(ordered, timeout_s=15.0, interval_s=0.2)


def test_the_tree_sorts_and_shows_hidden_files_from_its_menu_and_the_palette(
    page: Page, options_session: tuple[str, str]
) -> None:
    base_url, session_id = options_session
    sidebar = open_workspace(page, base_url, session_id)
    _open_app(page, sidebar, "Files")
    expect(sidebar.locator('[role="treeitem"][data-item-path="alpha.txt"]')).to_be_visible(
        timeout=30_000
    )
    _expect_order(sidebar, "alpha.txt", "zulu.txt")
    hidden = sidebar.locator('[role="treeitem"][data-item-path=".env"]')
    expect(hidden).to_be_visible()

    # The menu sorts by size, largest first, and hides hidden files.
    options = sidebar.get_by_role("button", name="Files tree options")
    options.click()
    page.get_by_role("menuitemradio", name="Size").click()
    _expect_order(sidebar, "zulu.txt", "alpha.txt")
    options.click()
    page.get_by_role("menuitemcheckbox", name="Show hidden files").click()
    expect(hidden).to_have_count(0)

    # ⌘K runs the same actions: it shows them again and sorts by name.
    run_palette(page, "Show Hidden Files")
    expect(hidden).to_be_visible(timeout=15_000)
    run_palette(page, "Sort Files Tree by Filename")
    _expect_order(sidebar, "alpha.txt", "zulu.txt")
