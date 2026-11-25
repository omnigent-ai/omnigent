"""E2E: the tabs sidebar's Files tree rows: folder chains, long names, tooltips and keys.

With ``workspace_sidebar_v2`` on, a chain of only-child folders, such as ``review/rules``,
reads as one ``review / rules`` row once the tree has listed ``review``. A deep, long file
name truncates in the middle, keeping its start and extension, with its full path in the
row's tooltip, and the tooltip follows the hovered row. Enter and Space open a file.
Seeded via the filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.files._files_page import (
    open_workspace,
    seeded_workspace,
    session_workspace,
    set_rail_width,
)
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app

_RULE = "config/review/rules/style.md"
_SETTINGS = "config/settings.toml"
_DEEP = "packages/app/src/components/navigation/keyboardShortcutsForTheReviewPanel.test.ts"
_SIBLING = "packages/app/src/components/navigation/index.ts"
# The one row of only-child folders, as each listing extends it.
_DEEP_CHAIN = [
    "packages/",
    "packages/app/",
    "packages/app/src/",
    "packages/app/src/components/",
    "packages/app/src/components/navigation/",
]


@pytest.fixture
def tree_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    files = {path: f"contents of {path}\n" for path in (_RULE, _SETTINGS, _DEEP, _SIBLING)}
    with seeded_workspace(seeded_session, files) as session:
        yield session


def _files_tree(page: Page, session: tuple[str, str]) -> Locator:
    base_url, session_id = session
    sidebar = open_workspace(page, base_url, session_id)
    _open_app(page, sidebar, "Files")
    expect(sidebar.get_by_role("treeitem", name="config", exact=True)).to_be_visible(
        timeout=30_000
    )
    return sidebar


def _row(sidebar: Locator, name: str) -> Locator:
    return sidebar.get_by_role("treeitem", name=name, exact=True)


_TOOLTIPS = 'div[style*="position: fixed"]'


def _tooltip(page: Page, text: str) -> Locator:
    """The row tooltip reading exactly ``text``; other fixed layers don't count."""
    return page.locator(_TOOLTIPS).filter(has_text=re.compile(f"^{re.escape(text)}$"))


# The row's name ends in its middle-truncated suffix segment; its counts and the actions
# button that shows on the hovered or focused row follow. Each box is measured against the
# tree's visible scroll area.
_ROW_NAME_GEOMETRY = """(row) => {
  const scroller = row.closest("[data-file-tree-scroll]");
  const box = (el) => {
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    return { left: rect.left, right: rect.right, width: rect.width, top: rect.top };
  };
  // Whether an element's text fits it whole, rather than its box merely having some width.
  const fits = (el) => !!el && el.scrollWidth <= el.clientWidth + 1;
  const suffix = row.querySelector(
    '[data-truncate-container="fruncate"] [data-truncate-content="visible"]'
  ) ?? row.querySelector('[data-item-section="content"] [data-truncate-content="visible"]');
  return {
    view: box(scroller),
    content: box(row.querySelector('[data-item-section="content"]')),
    suffix: box(suffix),
    suffixText: suffix?.textContent ?? "",
    suffixFits: fits(suffix),
    counts: box(row.querySelector('[data-item-section="decoration"]')),
    // The one actions button, only when it sits on this row.
    action: row.hasAttribute("data-item-has-menu-button")
      ? box(row.closest("[data-file-tree]")?.querySelector('[data-type="context-menu-trigger"]'))
      : null,
    sidewaysOverflow: scroller.scrollWidth - scroller.clientWidth,
  };
}"""


def _row_name_geometry(row: Locator) -> dict[str, Any]:
    """Where ``row`` draws its name's suffix, its counts and its menu lane."""
    return row.evaluate(_ROW_NAME_GEOMETRY)


# Room for a name's start, an ellipsis and a short extension, whatever the row's depth.
_MIN_NAME_WIDTH = 40


def _assert_row_name_readable(row: Locator, suffix: str) -> None:
    """The row's name ends in ``suffix``, drawn whole inside the tree, then its counts.

    The name keeps room for its start and its suffix's glyphs, so a row never shows its
    indentation and counts with no name between them.

    :param row: A file row of a file tree.
    :param suffix: The end of the name that must show, such as its extension.
    """
    geometry = _row_name_geometry(row)
    view, content, end = geometry["view"], geometry["content"], geometry["suffix"]
    slack = 0.5
    assert geometry["sidewaysOverflow"] <= 1, geometry
    assert geometry["suffixText"].endswith(suffix) and geometry["suffixFits"], geometry
    assert content["width"] >= _MIN_NAME_WIDTH, geometry
    assert view["left"] - slack <= end["left"] and end["right"] <= content["right"] + slack, (
        geometry
    )
    for lane in (geometry["counts"], geometry["action"]):
        if lane is None:
            continue
        assert content["right"] <= lane["left"] + slack, geometry
        assert lane["right"] <= view["right"] + slack, geometry


def test_single_child_folders_merge_into_one_row_and_expand(
    page: Page, tree_session: tuple[str, str]
) -> None:
    """``config/review/rules`` reads as ``review / rules`` and opens to its file."""
    sidebar = _files_tree(page, tree_session)
    _row(sidebar, "config").click()
    expect(_row(sidebar, "settings.toml")).to_be_visible(timeout=15_000)

    # Listing review/ shows it holds only rules/, so the two become one row.
    _row(sidebar, "review").click()
    merged = _row(sidebar, "review / rules")
    expect(merged).to_be_visible(timeout=15_000)
    expect(_row(sidebar, "rules")).to_have_count(0)
    if merged.get_attribute("aria-expanded") != "true":
        merged.click()
    expect(merged).to_have_attribute("aria-expanded", "true")
    expect(_row(sidebar, "style.md")).to_be_visible(timeout=15_000)

    # The tooltip names the hovered row's path, and follows the pointer to the next row.
    _row(sidebar, "config").hover()
    expect(_tooltip(page, "config")).to_have_count(1)
    _row(sidebar, "settings.toml").hover()
    expect(_tooltip(page, _SETTINGS)).to_have_count(1)
    expect(_tooltip(page, "config")).to_have_count(0)


def test_enter_and_space_open_a_file_and_back_returns_to_the_tree(
    page: Page, tree_session: tuple[str, str]
) -> None:
    """Enter and Space each open a row's file, one Back step from the tree.

    Opening the same place again is a no-op in the tab store, so whether a key also opens
    the file by the row button's click is checked in ``FilesTree``'s unit tests.
    """
    sidebar = _files_tree(page, tree_session)
    _row(sidebar, "config").click()
    settings = _row(sidebar, "settings.toml")
    breadcrumbs = sidebar.get_by_role("navigation", name="File path")
    back = sidebar.get_by_role("group", name="Workspace history").get_by_role(
        "button", name=re.compile("^Back")
    )
    for key in ("Enter", "Space"):
        expect(settings).to_be_visible(timeout=15_000)
        settings.focus()
        page.keyboard.press(key)
        expect(breadcrumbs).to_contain_text("settings.toml", timeout=15_000)
        back.click()
        expect(breadcrumbs).to_have_count(0)


def test_a_deep_long_name_keeps_its_extension_in_a_narrow_files_tree(
    page: Page, tree_session: tuple[str, str]
) -> None:
    """Deep in the tree, the file's row keeps its start and extension in a narrow tree."""
    sidebar = _files_tree(page, tree_session)
    # A narrow page moves the sidebar into a drawer, so narrow the sidebar itself.
    set_rail_width(page, sidebar, 320)
    # Each folder lists as it opens, so the chain row grows down to navigation/, then opens.
    for folder in _DEEP_CHAIN:
        chain = sidebar.locator(f'[role="treeitem"][data-item-path="{folder}"]')
        expect(chain).to_be_visible(timeout=15_000)
        chain.click()
    row = sidebar.locator(f'[role="treeitem"][data-item-path="{_DEEP}"]')
    expect(row).to_be_visible(timeout=15_000)
    _assert_row_name_readable(row, "ts")
    row.hover()
    expect(_tooltip(page, _DEEP)).to_have_count(1)


_BIG_FOLDER_FILES = 1200
# Scroll the tree that holds `row` to its end.
_SCROLL_TREE_TO_END = """(row) => {
  for (let node = row.parentElement; node; node = node.parentElement) {
    const scrolls = /(auto|scroll)/.test(getComputedStyle(node).overflowY);
    if (scrolls && node.scrollHeight > node.clientHeight + 4) {
      node.scrollTop = node.scrollHeight;
      return;
    }
  }
}"""


def test_a_folder_longer_than_one_listing_page_shows_its_last_file(
    page: Page, tree_session: tuple[str, str], request: pytest.FixtureRequest
) -> None:
    # More than the listing endpoint's 1,000-entry page, written straight into the workspace.
    base_url, session_id = tree_session
    big = session_workspace(base_url, session_id) / "big"
    big.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(big, ignore_errors=True))
    for index in range(_BIG_FOLDER_FILES):
        (big / f"f{index:04d}.txt").write_text(f"file {index}\n")
    sidebar = _files_tree(page, tree_session)
    _row(sidebar, "big").click()
    expect(_row(sidebar, "f0000.txt")).to_be_visible(timeout=30_000)

    # The tree is virtualized: scroll it to the end, from whichever row is rendered.
    last = _row(sidebar, f"f{_BIG_FOLDER_FILES - 1:04d}.txt")
    for _ in range(20):
        if last.count():
            break
        sidebar.get_by_role("treeitem").first.evaluate(_SCROLL_TREE_TO_END)
        page.wait_for_timeout(200)
    expect(last).to_be_visible(timeout=15_000)
