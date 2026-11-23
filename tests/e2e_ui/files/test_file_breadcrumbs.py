"""E2E: a file's path breadcrumbs on the tabs sidebar's Files page.

With ``workspace_sidebar_v2`` on, a file opened on the Files page shows its path as
breadcrumbs. Each segment lists the folder holding it with the segment selected, the
keyboard picks and opens an entry in the same tab, and the copy button copies the relative
path. A long path shows every folder, scrolled to the file name. Seeded via the filesystem
PUT endpoint (no agent run); the segments' trees and menus are in the unit tests
(``FileBreadcrumbs``, ``WorkspaceFolderTree``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.files._files_page import open_from_tree, open_workspace, seeded_workspace
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app

_LONG_PATH = "services/platform/ingestion/pipelines/streaming/handlers/event_router.py"
_FILES = ("src/pkg/alpha.py", "src/pkg/beta.py", "src/top.py", _LONG_PATH)


@pytest.fixture
def breadcrumb_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    with seeded_workspace(seeded_session, {path: f"# {path}\n" for path in _FILES}) as session:
        yield session


def _open_file(page: Page, sidebar: Locator, path: str) -> Locator:
    """Open ``path`` from the Files tab's tree; returns the Files page's breadcrumbs."""
    _open_app(page, sidebar, "Files")
    open_from_tree(sidebar, path)
    return sidebar.get_by_role("navigation", name="File path")


def test_breadcrumbs_browse_folders_open_siblings_and_copy_the_path(
    page: Page, breadcrumb_session: tuple[str, str]
) -> None:
    """Each segment lists its siblings with itself selected, and the keyboard opens an entry."""
    base_url, session_id = breadcrumb_session
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    sidebar = open_workspace(page, base_url, session_id)
    breadcrumbs = _open_file(page, sidebar, "src/pkg/alpha.py")
    expect(breadcrumbs).to_contain_text("src›pkg›alpha.py", timeout=30_000)

    # A folder lists the folder holding it, with the folder itself selected.
    breadcrumbs.get_by_role("button", name="pkg", exact=True).click()
    folder = page.get_by_role("dialog", name="Browse src")
    expect(folder.get_by_role("treeitem", name="top.py")).to_be_visible(timeout=30_000)
    pkg = folder.get_by_role("treeitem", name="pkg")
    expect(pkg).to_have_attribute("aria-selected", "true")
    expect(pkg).to_be_focused()
    page.keyboard.press("Escape")
    expect(folder).to_be_hidden()

    # The file name lists its siblings; the keyboard picks one and opens it in this tab.
    breadcrumbs.get_by_role("button", name="alpha.py", exact=True).click()
    siblings = page.get_by_role("dialog", name="Browse src/pkg")
    expect(siblings.get_by_role("treeitem", name="beta.py")).to_be_visible(timeout=30_000)
    expect(siblings.get_by_role("treeitem", name="alpha.py")).to_be_focused()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(siblings).to_be_hidden()
    expect(breadcrumbs).to_contain_text("src›pkg›beta.py", timeout=15_000)

    # The root segment lists the workspace root; the keyboard drills into it.
    breadcrumbs.get_by_role("button", name="src", exact=True).click()
    root = page.get_by_role("dialog", name=re.compile(r"^Browse "))
    src = root.get_by_role("treeitem", name="src")
    expect(src).to_be_focused(timeout=30_000)
    page.keyboard.press("ArrowRight")
    expect(root.get_by_role("treeitem", name="top.py")).to_be_visible(timeout=30_000)
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    expect(root.get_by_role("treeitem", name="top.py")).to_be_focused()
    page.keyboard.press("Enter")
    expect(root).to_be_hidden()
    expect(breadcrumbs).to_contain_text("src›top.py", timeout=15_000)

    # The copy button copies the relative path shown and confirms it in place.
    copy = breadcrumbs.get_by_role("button", name="Copy relative path: top.py")
    copy.hover()
    expect(page.get_by_role("tooltip")).to_have_text("Copy relative path")
    copy.click()
    expect(page.get_by_role("tooltip")).to_have_text("Copied")
    assert page.evaluate("navigator.clipboard.readText()") == "src/top.py"


# Whether a segment shows inside the breadcrumbs' scrolling strip: all of it, or its
# start when it's wider than the strip itself.
_SHOWN_IN_STRIP_JS = """element => {
  const strip = element.closest("[data-tab-strip-viewport]");
  const box = element.getBoundingClientRect();
  const view = strip.getBoundingClientRect();
  const startShows = box.left >= view.left - 0.5;
  return startShows && (box.right <= view.right + 0.5 || box.width > view.width);
}"""


def test_breadcrumbs_show_a_long_path_in_full_and_browse_it_by_keyboard(
    page: Page, breadcrumb_session: tuple[str, str]
) -> None:
    """Every folder shows, scrolled to the file name; the keyboard reaches and opens each one."""
    base_url, session_id = breadcrumb_session
    sidebar = open_workspace(page, base_url, session_id)
    breadcrumbs = _open_file(page, sidebar, _LONG_PATH)
    expect(breadcrumbs).to_contain_text(
        "services›platform›ingestion›pipelines›streaming›handlers›event_router.py",
        timeout=30_000,
    )
    segment = lambda name: breadcrumbs.get_by_role("button", name=name, exact=True)  # noqa: E731

    # The strip scrolls to the file name, and the copy button stays beside it.
    file_name = segment("event_router.py")
    page.wait_for_function(_SHOWN_IN_STRIP_JS, arg=file_name.element_handle(), timeout=10_000)
    expect(
        breadcrumbs.get_by_role("button", name=re.compile("^Copy relative path"))
    ).to_be_in_viewport(ratio=1)

    # Tabbing back reaches a folder the strip had scrolled away, and shows it.
    file_name.focus()
    for _ in range(6):
        page.keyboard.press("Shift+Tab")
    services = segment("services")
    expect(services).to_be_focused()
    page.wait_for_function(_SHOWN_IN_STRIP_JS, arg=services.element_handle(), timeout=10_000)

    # Enter opens a folder's tree; Escape closes it and returns to the folder.
    page.keyboard.press("Enter")
    root = page.get_by_role("dialog", name=re.compile(r"^Browse "))
    expect(root.get_by_role("treeitem", name="services")).to_be_focused(timeout=30_000)
    page.keyboard.press("Escape")
    expect(root).to_be_hidden()
    expect(services).to_be_focused()

    # Space opens a middle folder's siblings, the folder itself selected.
    segment("ingestion").focus()
    page.keyboard.press(" ")
    folder = page.get_by_role("dialog", name="Browse services/platform")
    ingestion = folder.get_by_role("treeitem", name="ingestion")
    expect(ingestion).to_be_focused(timeout=30_000)
    expect(ingestion).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Escape")
    expect(folder).to_be_hidden()
