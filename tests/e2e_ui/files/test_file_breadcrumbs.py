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
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app, _set_flag

_DESKTOP = {"width": 1440, "height": 900}
_LONG_PATH = "services/platform/ingestion/pipelines/streaming/handlers/event_router.py"
_FILES = ("src/pkg/alpha.py", "src/pkg/beta.py", "src/top.py", _LONG_PATH)


def _session_workspace(base_url: str, session_id: str) -> Path:
    """The session's workspace on its host, where the filesystem PUTs write."""
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/shell",
        json={"command": "pwd", "timeout": 30},
        timeout=60.0,
    )
    response.raise_for_status()
    return Path(response.json()["stdout"].strip().splitlines()[-1])


@pytest.fixture
def breadcrumb_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    for path in _FILES:
        httpx.put(
            f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
            json={"content": f"# {path}\n", "encoding": "utf-8"},
            timeout=10.0,
        ).raise_for_status()
    workspace = _session_workspace(base_url, session_id)
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _mac(page: Page) -> bool:
    return bool(page.evaluate("() => /Mac|iPhone|iPad/.test(navigator.platform)"))


def _open_workspace(page: Page, base_url: str, session_id: str) -> Locator:
    """Open the session with ``workspace_sidebar_v2`` on; returns its Workspace sidebar."""
    _set_flag(page, True)
    page.set_viewport_size(_DESKTOP)
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    sidebar = page.get_by_role("complementary", name="Workspace")
    expect(sidebar.get_by_role("group", name="Workspace history")).to_be_visible(timeout=60_000)
    return sidebar


def _open_from_tree(tree: Locator, path: str) -> None:
    """Open the folders above ``path`` in a Files tree, then the file itself."""
    parts = path.split("/")
    for depth in range(1, len(parts)):
        folder_path = "/".join(parts[:depth]) + "/"
        folder = tree.locator(f'[role="treeitem"][data-item-path="{folder_path}"]')
        expect(folder).to_be_visible(timeout=30_000)
        if folder.get_attribute("aria-expanded") != "true":
            folder.click()
    row = tree.locator(f'[role="treeitem"][data-item-path="{path}"]')
    expect(row).to_be_visible(timeout=30_000)
    row.click()


def _open_file(page: Page, sidebar: Locator, path: str) -> Locator:
    """Open ``path`` from the Files tab's tree; returns the Files page's breadcrumbs."""
    _open_app(page, sidebar, "Files")
    _open_from_tree(sidebar, path)
    return sidebar.get_by_role("navigation", name="File path")


def test_breadcrumbs_browse_folders_open_siblings_and_copy_the_path(
    page: Page, breadcrumb_session: tuple[str, str]
) -> None:
    """Each segment lists its siblings with itself selected, and the keyboard opens an entry."""
    base_url, session_id = breadcrumb_session
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    sidebar = _open_workspace(page, base_url, session_id)
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


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_breadcrumbs_show_a_long_path_in_full_and_browse_it_by_keyboard(
    page: Page, breadcrumb_session: tuple[str, str], color_scheme: str
) -> None:
    """Every folder shows, scrolled to the file name; the keyboard reaches and opens each one."""
    base_url, session_id = breadcrumb_session
    page.emulate_media(color_scheme=color_scheme)
    sidebar = _open_workspace(page, base_url, session_id)
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
