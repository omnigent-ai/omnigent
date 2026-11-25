"""E2E: the tabs sidebar's CodeMirror file viewer and editor.

With ``workspace_sidebar_v2`` on, the Files page shows a file in CodeMirror. ⌥-drag
selects a column, one cursor per line, and ⌘K's Column select commands grow the box from
the keyboard. A read-only file highlights matching brackets, which only an editable file
did before. Indented lines show a guide at each level, with the cursor's block drawn
stronger. Seeded via the filesystem PUT endpoint (no agent run); the edge cases are in
the unit tests (``columnSelection``, ``codeMirrorBracketMatching``, ``indentGuides``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.files._files_page import (
    open_from_tree,
    open_workspace,
    remove_seeded,
    run_palette,
    seeded_workspace,
    session_workspace,
)
from tests.e2e_ui.files.test_file_autosave import _wait_for_persisted
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app

_COLUMNS_PATH = "src/columns.py"
_COLUMNS_SOURCE = "alpha = 1\nbeta = 2\ngamma = 3\ndelta = 4\n"
_BLOCKS_PATH = "src/blocks.py"
_BLOCKS_SOURCE = "def run(ready):\n    if ready:\n        go()\n    return ready\n"
_SEEDED = {_COLUMNS_PATH: _COLUMNS_SOURCE, _BLOCKS_PATH: _BLOCKS_SOURCE}
# Larger than the server's 10 MiB read cap, so the file arrives truncated and opens read-only.
_LARGE_PATH = "src/generated_tables.ts"
_LARGE_SOURCE = "\n".join(
    f"export const entry{index:06d} = {{ id: {index}, label: 'row {index}' }};"
    for index in range(240_000)
)


@pytest.fixture
def code_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    workspace = session_workspace(base_url, session_id)
    # The large file takes longer to write than the shared helper waits.
    httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{_LARGE_PATH}",
        json={"content": _LARGE_SOURCE, "encoding": "utf-8"},
        timeout=60.0,
    ).raise_for_status()
    try:
        with seeded_workspace(seeded_session, _SEEDED) as session:
            yield session
    finally:
        remove_seeded(workspace, [_LARGE_PATH])


def _open_file(page: Page, base_url: str, session_id: str, path: str) -> Locator:
    """Opens ``path`` from the Files page's tree and returns its editor."""
    sidebar = open_workspace(page, base_url, session_id)
    _open_app(page, sidebar, "Files")
    open_from_tree(sidebar, path)
    editor = sidebar.locator('[data-testid="workspace-files-place"]:visible .cm-editor')
    expect(editor).to_be_visible(timeout=30_000)
    return editor


def test_alt_drag_selects_a_column_and_the_palette_grows_it(
    page: Page, code_session: tuple[str, str]
) -> None:
    """⌥-drag puts a cursor on each line it crosses, and Column select up is a command."""
    base_url, session_id = code_session
    editor = _open_file(page, base_url, session_id, _COLUMNS_PATH)
    lines = editor.locator(".cm-line")
    expect(lines.nth(3)).to_have_text("delta = 4", timeout=30_000)

    lines.nth(0).click()
    start = lines.nth(0).bounding_box()
    end = lines.nth(2).bounding_box()
    assert start is not None and end is not None
    page.keyboard.down("Alt")
    page.mouse.move(start["x"] + 1, start["y"] + start["height"] / 2)
    page.mouse.down()
    page.mouse.move(end["x"] + 1, end["y"] + end["height"] / 2, steps=6)
    page.mouse.up()
    page.keyboard.up("Alt")
    page.keyboard.type("# ")
    expect(lines).to_have_text(["# alpha = 1", "# beta = 2", "# gamma = 3", "delta = 4", ""])

    # From one cursor, the palette's Column select up grows a box over the line above.
    lines.nth(3).click(position={"x": 1, "y": 5})
    run_palette(page, "Column select up")
    page.keyboard.type("! ")
    expect(lines).to_have_text(
        ["# alpha = 1", "# beta = 2", "! # gamma = 3", "! delta = 4", ""], timeout=10_000
    )
    _wait_for_persisted(base_url, session_id, _COLUMNS_PATH, "! delta = 4")


def test_a_read_only_file_highlights_matching_brackets(
    page: Page, code_session: tuple[str, str]
) -> None:
    base_url, session_id = code_session
    editor = _open_file(page, base_url, session_id, _LARGE_PATH)
    line = editor.locator(".cm-line").first
    expect(line).to_contain_text("entry000000", timeout=60_000)

    # With the cursor beside a brace, the brace it pairs with is highlighted too.
    line.click()
    page.keyboard.press("End")
    page.keyboard.press("ArrowLeft")
    # The truncated file takes no typing.
    page.keyboard.type("x")
    expect(line).to_have_text("export const entry000000 = { id: 0, label: 'row 0' };")
    expect(editor.locator(".cm-matchingBracket")).to_have_text(["{", "}"])


def test_indented_lines_show_guides_with_the_cursors_block_drawn_stronger(
    page: Page, code_session: tuple[str, str]
) -> None:
    base_url, session_id = code_session
    editor = _open_file(page, base_url, session_id, _BLOCKS_PATH)
    lines = editor.locator(".cm-line")
    expect(lines.nth(2)).to_have_text("        go()", timeout=30_000)
    guides = re.compile(r"\bcm-indent-guides\b")
    active = re.compile(r"\bcm-indent-guide-active\b")
    expect(lines.nth(0)).not_to_have_class(guides)
    for indented in (1, 2, 3):
        expect(lines.nth(indented)).to_have_class(guides)

    # The cursor inside the if block marks that block's guide on its lines only.
    lines.nth(2).click()
    expect(lines.nth(2)).to_have_class(active)
    expect(lines.nth(2)).to_have_attribute("style", re.compile(r"--cm-indent-guides:\s*2"))
