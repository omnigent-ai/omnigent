"""E2E: browser journeys for the tabs sidebar's CodeMirror viewer."""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui._quick_pick_palette import run_palette_command
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
# Three nested scopes with a long body, so their opening lines scroll off the top.
_NESTED_PATH = "src/inventory.py"
_NESTED_SOURCE = "\n".join(
    [
        "class Inventory:",
        "    def restock(self, items):",
        "        for item in items:",
        *(f"            self.count[item] += {index}" for index in range(1, 121)),
        "        return self.count",
        "",
        "def unrelated():",
        "    return None",
    ]
)
_MENU_PATH = "src/menu.py"
_MENU_SOURCE = 'def greet(name):\n    return "hello " + name\n'
_SCOPE_PATH = "src/scope.py"
_SCOPE_SOURCE = "total = 0\ntotal += 1\ntotal += 2\nprint(total)\n"
# Larger than the server's 10 MiB read cap, so the file arrives truncated and opens read-only.
_LARGE_PATH = "src/generated_tables.ts"


_SEEDED = {
    _COLUMNS_PATH: _COLUMNS_SOURCE,
    _NESTED_PATH: _NESTED_SOURCE,
    _MENU_PATH: _MENU_SOURCE,
    _SCOPE_PATH: _SCOPE_SOURCE,
}


@pytest.fixture
def code_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    with seeded_workspace(seeded_session, _SEEDED) as session:
        yield session


@pytest.fixture
def large_code_session(code_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Also writes the file past the viewer's truncation limit, for the one journey opening it."""
    base_url, session_id = code_session
    workspace = session_workspace(base_url, session_id)
    large_source = "\n".join(
        f"export const entry{index:06d} = {{ id: {index}, label: 'row {index}' }};"
        for index in range(240_000)
    )
    # The large file takes longer to write than the shared helper waits.
    try:
        httpx.put(
            f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{_LARGE_PATH}",
            json={"content": large_source, "encoding": "utf-8"},
            timeout=60.0,
        ).raise_for_status()
        yield code_session
    finally:
        remove_seeded(workspace, [_LARGE_PATH])


def _open_file(
    page: Page, base_url: str, session_id: str, path: str, *, quick_pick: bool = False
) -> Locator:
    """Opens ``path`` from the Files page's tree and returns its editor."""
    sidebar = open_workspace(page, base_url, session_id, quick_pick=quick_pick)
    _open_app(page, sidebar, "Files")
    open_from_tree(sidebar, path)
    editor = sidebar.locator('[data-testid="workspace-files-place"]:visible .cm-editor')
    expect(editor).to_be_visible(timeout=30_000)
    return editor


def test_alt_drag_selects_a_column_and_the_palette_grows_it(
    page: Page, code_session: tuple[str, str]
) -> None:
    """⌥-drag puts a cursor on each line it crosses, and the Column select commands grow it."""
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
    # Column select down grows the dragged box itself, not only its last line.
    run_palette(page, "Column select down")
    page.keyboard.type("# ")
    expect(lines).to_have_text(
        ["# alpha = 1", "# beta = 2", "# gamma = 3", "# delta = 4", ""], timeout=10_000
    )

    # From one cursor, the palette's Column select up grows a box over the line above.
    lines.nth(3).click(position={"x": 1, "y": 5})
    run_palette(page, "Column select up")
    page.keyboard.type("! ")
    expect(lines).to_have_text(
        ["# alpha = 1", "# beta = 2", "! # gamma = 3", "! # delta = 4", ""], timeout=10_000
    )
    _wait_for_persisted(base_url, session_id, _COLUMNS_PATH, "! # delta = 4")


def test_a_read_only_file_highlights_matching_brackets(
    page: Page, large_code_session: tuple[str, str]
) -> None:
    base_url, session_id = large_code_session
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


# How far the first pinned line sits below the scroller's top edge.
_STICKY_GAP = """(scroller) => {
    const row = scroller.querySelector(".cm-stickyScroll-line");
    return row.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
}"""


def test_a_nested_block_pins_its_scopes_and_a_click_scrolls_back_to_one(
    page: Page, code_session: tuple[str, str]
) -> None:
    base_url, session_id = code_session
    editor = _open_file(page, base_url, session_id, _NESTED_PATH)
    expect(editor.locator(".cm-line").first).to_have_text("class Inventory:", timeout=30_000)
    scroller = editor.locator(".cm-scroller")
    pinned = editor.locator(".cm-stickyScroll-line")
    expect(pinned).to_have_count(0)

    scroller.evaluate("(node) => { node.scrollTop = 1500; }")
    expect(pinned).to_have_count(3)
    expect(pinned.nth(0)).to_have_text(re.compile(r"^1\s*class Inventory:"))
    expect(pinned.nth(1)).to_have_text(re.compile(r"^2\s*def restock\(self, items\):"))
    expect(pinned.nth(2)).to_have_text(re.compile(r"^3\s*for item in items:"))
    # The pinned lines keep their syntax colors, flush with the top so no text shows above.
    expect(pinned.nth(0).locator(".cm-shiki-token").first).to_be_visible()
    gap = scroller.evaluate(_STICKY_GAP)
    assert abs(gap) < 0.5, f"the pinned lines sit {gap}px below the top"

    # Line 1 opens no scope, so clicking it scrolls to the very top with the cursor on it.
    pinned.nth(0).click()
    page.wait_for_function("(node) => node.scrollTop < 10", arg=scroller.element_handle())
    expect(pinned).to_have_count(0)
    expect(editor.locator(".cm-activeLine")).to_have_text("class Inventory:")

    # A right-click on a pinned row targets that line too, not the hidden text under it.
    scroller.evaluate("(node) => { node.scrollTop = 1500; }")
    expect(pinned).to_have_count(3)
    pinned.nth(1).click(button="right")
    menu = page.get_by_test_id("file-editor-context-menu")
    expect(menu).to_be_visible()
    expect(editor.locator(".cm-activeLine")).to_have_text(re.compile(r"def restock"))
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    # Line 2 goes back under line 1's row, which is the top of the file here.
    page.wait_for_function("(node) => node.scrollTop < 10", arg=scroller.element_handle())


def test_sticky_scroll_turns_off_from_the_palette_while_the_file_is_open(
    page: Page, code_session: tuple[str, str]
) -> None:
    """⌘K's Sticky scroll setting unpins the open file's scopes, and stays off after a reload."""
    base_url, session_id = code_session
    editor = _open_file(page, base_url, session_id, _NESTED_PATH, quick_pick=True)
    expect(editor.locator(".cm-line").first).to_have_text("class Inventory:", timeout=30_000)
    pinned = editor.locator(".cm-stickyScroll-line")
    editor.locator(".cm-scroller").evaluate("(node) => { node.scrollTop = 1500; }")
    expect(pinned).to_have_count(3)

    run_palette_command(page, "sticky scroll", "Sticky scroll")
    page.get_by_role("dialog").get_by_role("option", name="Off").click()
    expect(pinned).to_have_count(0)
    saved = page.evaluate("localStorage.getItem('omnigent:file-view-preferences')")
    assert '"stickyScroll":false' in saved, saved

    # After a reload the saved choice pins nothing, however deep the text scrolls: the line
    # at the top of the view is inside the loop's body, given the frames pins take to appear.
    page.reload()
    expect(editor.locator(".cm-line").first).to_have_text("class Inventory:", timeout=30_000)
    top_line = editor.locator(".cm-scroller").evaluate(
        """async (node) => {
            node.scrollTop = 1500;
            for (let frame = 0; frame < 5; frame++) await new Promise(requestAnimationFrame);
            const box = node.querySelector(".cm-content").getBoundingClientRect();
            const top = node.getBoundingClientRect().top;
            const at = document.elementFromPoint(box.left + 40, top + 4);
            return at?.closest(".cm-line")?.textContent ?? "";
        }"""
    )
    assert "self.count[item] +=" in top_line, top_line
    expect(pinned).to_have_count(0)


def test_right_click_lists_the_editors_commands(page: Page, code_session: tuple[str, str]) -> None:
    """Right-click lists the editor's commands; Toggle line comment acts on the clicked line."""
    base_url, session_id = code_session
    editor = _open_file(page, base_url, session_id, _MENU_PATH)
    lines = editor.locator(".cm-line")
    expect(lines.nth(1)).to_have_text('    return "hello " + name', timeout=30_000)
    menu = page.get_by_test_id("file-editor-context-menu")

    # Right-click moves the cursor to the line, and Toggle line comment acts there.
    lines.nth(1).click(button="right")
    expect(menu).to_be_visible()
    menu.get_by_role("menuitem", name=re.compile("^Toggle line comment")).click()
    expect(menu).to_have_count(0)
    expect(lines.nth(1)).to_have_text('    # return "hello " + name')
    expect(editor.locator(".cm-content")).to_be_focused()

    _wait_for_persisted(base_url, session_id, _MENU_PATH, '    # return "hello " + name')


def test_find_in_selection_counts_and_replaces_only_inside_it(
    page: Page, code_session: tuple[str, str]
) -> None:
    """Find in selection counts and replaces only inside the lines selected when it turned on."""
    base_url, session_id = code_session
    editor = _open_file(page, base_url, session_id, _SCOPE_PATH)
    lines = editor.locator(".cm-line")
    expect(lines.nth(3)).to_have_text("print(total)", timeout=30_000)

    # Select the middle two lines, then search for the variable they share with the rest.
    lines.nth(1).click(position={"x": 1, "y": 5})
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Shift+End")
    page.keyboard.press("ControlOrMeta+Alt+Shift+f")
    page.get_by_test_id("file-find-input").fill("total")
    status = page.get_by_test_id("file-find-status")
    expect(status).to_have_text("1 of 4")

    toggle = page.get_by_role("button", name="Find in selection")
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")
    # It moves to the selection's first match, and the matches outside lose their highlight.
    expect(status).to_have_text("1 of 2")
    expect(editor.locator(".cm-searchMatch-selected")).to_have_count(1)
    expect(editor.locator(".cm-find-outside-scope .cm-searchMatch-selected")).to_have_count(0)
    expect(editor.locator(".cm-find-scope").first).to_be_visible()
    outside = editor.locator(".cm-find-outside-scope .cm-searchMatch")
    expect(outside).to_have_count(2)
    expect(outside.first).to_have_css("background-color", "rgba(0, 0, 0, 0)")

    replacement = page.get_by_role("textbox", name="Replace with")
    replacement.fill("count")
    replacement.press("ControlOrMeta+Enter")
    expect(lines).to_have_text(["total = 0", "count += 1", "count += 2", "print(total)", ""])
    _wait_for_persisted(base_url, session_id, _SCOPE_PATH, "count += 2")

    # Turning it off searches the whole file again.
    toggle.click()
    expect(status).to_have_text(re.compile(r"^\d of 2$"))
