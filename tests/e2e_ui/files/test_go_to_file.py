"""E2E: Go to File and its Text tab search the session runner's real workspace from the browser,
and open what they find in the tabs sidebar."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.files._files_page import is_mac, open_workspace, seeded_workspace
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app

_FILES = ("widgets/gadget.py", "widgets/gizmo.py", "README.md")
# gadget.py's own line comes 40th, so opening its match at the top of the file would show.
_MATCH_LINE = 40
_CONTENTS = {path: f"contents of {path}\n" for path in _FILES} | {
    "widgets/gadget.py": "".join(f"filler {line}\n" for line in range(1, _MATCH_LINE))
    + "contents of widgets/gadget.py\n"
    + "".join(f"filler {line}\n" for line in range(_MATCH_LINE + 1, 80)),
}


@pytest.fixture
def go_to_file_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    # Removes exactly what it wrote from the session's real workspace afterwards.
    with seeded_workspace(seeded_session, _CONTENTS):
        yield seeded_session


def _go_to_file_chord(page: Page) -> str:
    """``palette.files``: ⌃⌘P on the Mac, Ctrl+Alt+Shift+P elsewhere."""
    return "Control+Meta+p" if is_mac(page) else "Control+Alt+Shift+p"


def _text_search_chord(page: Page) -> str:
    """``palette.text``: ⌃⌘O on the Mac, Ctrl+Alt+O elsewhere."""
    return "Control+Meta+o" if is_mac(page) else "Control+Alt+o"


def test_go_to_file_opens_a_match_from_every_entry_point_and_lists_it_next_time(
    page: Page, go_to_file_session: tuple[str, str]
) -> None:
    base_url, session_id = go_to_file_session
    sidebar = open_workspace(page, base_url, session_id)
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
    # The active match is previewed beside the list, from its first line.
    expect(page.get_by_test_id("go-to-file-preview")).to_contain_text("filler 1", timeout=15_000)
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
    sidebar = open_workspace(page, base_url, session_id)
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
    expect(page.get_by_test_id("go-to-file-preview")).to_contain_text(
        f"Line {_MATCH_LINE}", timeout=15_000
    )
    query.press("Enter")
    expect(query).to_be_hidden()
    expect(sidebar.get_by_role("tab", name=re.compile(r"^gadget\.py"))).to_have_attribute(
        "aria-selected", "true", timeout=15_000
    )
    # The file opens with the cursor on the matched line, not at the top.
    editor = sidebar.locator('[data-testid="workspace-files-place"]:visible .cm-editor')
    expect(editor.locator(".cm-activeLine")).to_have_text(
        "contents of widgets/gadget.py", timeout=15_000
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
