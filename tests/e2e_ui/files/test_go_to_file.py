"""E2E: Go to File finds a workspace file from the keyboard and opens it in the tabs sidebar.

With ``workspace_sidebar_v2`` on, the Files page's "Go to file" button, ⌘K's "Go to File in
Workspace…" row and its chord open one dialog. The workspace's own search answers each
query, and the active match is previewed beside the list; a file opened from anywhere is
listed first the next time. Its Text tab, which the text-search chord opens, finds lines and
opens a file at one, or hands the search to the sidebar's Search page. Ranking and the
dialog's states are in the unit tests (``goToFileView``, ``textSearchView``, ``GoToFileHost``).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app, _set_flag

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FILES = ("widgets/gadget.py", "widgets/gizmo.py", "README.md")
_DESKTOP = {"width": 1440, "height": 900}


@pytest.fixture
def go_to_file_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    for path in _FILES:
        httpx.put(
            f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
            json={"content": f"contents of {path}\n", "encoding": "utf-8"},
            timeout=10.0,
        ).raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _mac(page: Page) -> bool:
    return bool(page.evaluate("() => /Mac|iPhone|iPad/.test(navigator.platform)"))


def _go_to_file_chord(page: Page) -> str:
    """``palette.files``: ⌃⌘P on the Mac, Ctrl+Alt+P elsewhere."""
    return "Control+Meta+p" if _mac(page) else "Control+Alt+p"


def _text_search_chord(page: Page) -> str:
    """``palette.text``: ⌃⌘O on the Mac, Ctrl+Alt+F elsewhere."""
    return "Control+Meta+o" if _mac(page) else "Control+Alt+f"


def _set_content_search(page: Page) -> None:
    """Answer ``/v1/info`` with ``workspace_content_search`` on too, for the Search page."""

    def answer(route: Route) -> None:
        response = route.fetch()
        info = response.json()
        info["features"] = {
            **(info.get("features") or {}),
            "workspace_sidebar_v2": True,
            "workspace_content_search": True,
        }
        route.fulfill(response=response, json=info)

    page.route("**/v1/info", answer)


def _open_workspace(
    page: Page, base_url: str, session_id: str, *, content_search: bool = False
) -> Locator:
    if content_search:
        _set_content_search(page)
    else:
        _set_flag(page, True)
    page.set_viewport_size(_DESKTOP)
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    sidebar = page.get_by_role("complementary", name="Workspace")
    expect(sidebar.get_by_role("group", name="Workspace history")).to_be_visible(timeout=60_000)
    return sidebar


def test_go_to_file_opens_a_match_from_every_entry_point_and_lists_it_next_time(
    page: Page, go_to_file_session: tuple[str, str]
) -> None:
    base_url, session_id = go_to_file_session
    sidebar = _open_workspace(page, base_url, session_id)
    query = page.get_by_role("combobox", name="Go to a file")

    # A narrow Files tab with no file open is the tree; hidden, the page offers Go to file,
    # which opens the finder on its query.
    _open_app(page, sidebar, "Files")
    expect(sidebar.get_by_role("treeitem", name="README.md")).to_be_visible(timeout=30_000)
    sidebar.get_by_role("button", name="Hide Files Tree").click()
    expect(sidebar.get_by_text("Open a file")).to_be_visible()
    button = sidebar.get_by_role("button", name=re.compile(r"^Go to file"))
    expect(button).to_be_enabled(timeout=30_000)
    button.click()
    expect(query).to_be_focused()
    query.fill("gadget")
    match = page.get_by_role("option", name=re.compile(r"gadget\.py"))
    expect(match).to_be_visible(timeout=15_000)
    expect(page.get_by_role("option", name=re.compile(r"gizmo\.py"))).to_have_count(0)
    # The finder marks where the typed text occurs.
    expect(match.locator('mark[data-match-highlight="strong"]')).to_have_text(["gadget"])
    # The active match is previewed beside the list.
    expect(page.get_by_test_id("go-to-file-preview")).to_contain_text(
        "contents of widgets/gadget.py", timeout=15_000
    )
    query.press("Enter")
    expect(query).to_be_hidden()
    expect(sidebar.get_by_role("tab", name=re.compile(r"^gadget\.py"))).to_have_attribute(
        "aria-selected", "true", timeout=15_000
    )

    # Its chord reopens it with nothing typed, listing the file just opened, and closes it.
    page.keyboard.press(_go_to_file_chord(page))
    expect(query).to_be_focused()
    expect(query).to_have_value("")
    expect(page.get_by_text("Recently opened")).to_be_visible()
    expect(page.get_by_role("option", name=re.compile(r"gadget\.py"))).to_be_visible()
    page.keyboard.press(_go_to_file_chord(page))
    expect(query).to_be_hidden()

    # ⌘K's row closes the palette and opens the same finder.
    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_test_id("command-palette-input")
    expect(palette).to_be_focused(timeout=10_000)
    palette.fill("Go to File in Workspace")
    page.get_by_role("option", name=re.compile("Go to File in Workspace")).click()
    expect(palette).to_have_count(0)
    expect(query).to_be_focused()
    query.fill("gizmo")
    expect(page.get_by_role("option", name=re.compile(r"gizmo\.py"))).to_be_visible(timeout=15_000)
    query.press("Enter")
    expect(sidebar.get_by_role("tab", name=re.compile(r"^gizmo\.py"))).to_have_attribute(
        "aria-selected", "true", timeout=15_000
    )


def test_text_search_finds_a_line_opens_it_there_and_hands_off_to_the_search_page(
    page: Page, go_to_file_session: tuple[str, str]
) -> None:
    """Where the session's files can be searched for text, Go to File's Text tab searches what was
    typed in Files, and the text-search chord opens it on Text."""
    base_url, session_id = go_to_file_session
    sidebar = _open_workspace(page, base_url, session_id, content_search=True)
    query = page.get_by_role("combobox", name="Search text in files")

    # Typed in Files, the same text searches the files' contents once the Text tab is clicked.
    page.keyboard.press(_go_to_file_chord(page))
    files_query = page.get_by_role("combobox", name="Go to a file")
    expect(files_query).to_be_focused(timeout=15_000)
    files_query.fill("gadget")
    expect(page.get_by_role("option", name=re.compile(r"gadget\.py"))).to_be_visible(
        timeout=15_000
    )
    page.get_by_role("tab", name=re.compile("Text")).click()
    expect(query).to_be_focused()
    expect(query).to_have_value("gadget")
    expect(page.get_by_role("group", name="widgets/gadget.py")).to_be_visible(timeout=15_000)
    page.keyboard.press("Escape")
    expect(query).to_be_hidden()

    page.keyboard.press(_text_search_chord(page))
    expect(query).to_be_focused(timeout=15_000)
    expect(page.get_by_role("tab", name=re.compile("Text"))).to_have_attribute(
        "aria-selected", "true"
    )
    query.fill("contents of widgets/gadget")
    # Playwright can't carry a "/" inside a role name's pattern, so match the file's name.
    line = page.get_by_role("option", name=re.compile(r"gadget\.py"))
    expect(line).to_be_visible(timeout=15_000)
    # The line is listed under its file and previewed at its line.
    expect(page.get_by_role("group", name="widgets/gadget.py")).to_be_visible()
    expect(page.get_by_test_id("go-to-file-preview")).to_contain_text("Line 1", timeout=15_000)
    query.press("Enter")
    expect(query).to_be_hidden()
    expect(sidebar.get_by_role("tab", name=re.compile(r"^gadget\.py"))).to_have_attribute(
        "aria-selected", "true", timeout=15_000
    )

    # Open in Search hands the search and its lines to the Search page.
    page.keyboard.press(_text_search_chord(page))
    query.fill("contents of widgets")
    expect(page.get_by_role("option", name=re.compile(r"gizmo\.py"))).to_be_visible(timeout=15_000)
    page.get_by_role("button", name=re.compile("Open in Search")).click()
    expect(query).to_be_hidden()
    field = sidebar.get_by_role("searchbox", name="Search file contents")
    expect(field).to_be_focused(timeout=15_000)
    expect(field).to_have_value("contents of widgets")
    expect(sidebar.get_by_role("button", name="Open widgets/gizmo.py at line 1")).to_be_visible()
