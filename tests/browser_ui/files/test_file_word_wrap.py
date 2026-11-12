"""The View settings "Wrap lines" toggle soft-wraps long lines in real Monaco."""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect

from tests._helpers.polling import poll_until
from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _mock_markdown_files,
)
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture

_FILE_PATH = "src/ProxyHandler.scala"
_TAIL_MARKER = "endOfTheLongComment"
_EDIT_MARKER = "typedBeforeWrapping"
_LONG_COMMENT = (
    "  // Forwards each MCP request to the upstream service, retrying idempotent calls "
    "with exponential backoff, and records latency, status, and the caller's workspace "
    "so that slow or failing upstreams can be found from the proxy metrics dashboard "
    f"without reading raw logs {_TAIL_MARKER}"
)
_FILE_CONTENT = "\n".join(
    [
        "object ProxyHandler {",
        _LONG_COMMENT,
        "  def handle(request: Request): Response = forward(request)",
        "}",
    ]
)


def _open_long_line_file(page: Page, session: BrowserSession) -> Locator:
    """Open the seeded file with default view preferences; return its Monaco editor."""
    _mock_markdown_files(page, session, {_FILE_PATH: _FILE_CONTENT})
    base_url, session_id = session
    page.set_viewport_size({"width": 1600, "height": 1000})
    page.goto(f"{base_url}/c/{session_id}?file={_FILE_PATH}")
    editor = page.locator('[data-testid="file-viewer"]:visible .monaco-editor')
    expect(editor.locator(".view-line", has_text="def handle")).to_be_visible(timeout=30_000)
    return editor


def _tail_fits_in_editor(editor: Locator) -> bool:
    """Whether the long line's last word ends inside the editor's visible width."""
    tail = editor.locator(".view-line span span", has_text=_TAIL_MARKER).last
    tail.wait_for(state="attached")
    editor_box = editor.bounding_box()
    tail_box = tail.bounding_box()
    assert editor_box is not None and tail_box is not None
    return tail_box["x"] + tail_box["width"] <= editor_box["x"] + editor_box["width"]


def _toggle_wrap_lines(page: Page) -> None:
    page.locator('[data-testid="file-viewer"]:visible').get_by_role(
        "button", name="View settings"
    ).click()
    page.get_by_role("menuitem", name="Wrap lines").click()
    page.keyboard.press("Escape")


def test_wrap_lines_soft_wraps_a_long_code_line(
    page: Page, seeded_session: BrowserSession
) -> None:
    """Wrapping brings a long line's end into view and keeps the unsaved edit and selection."""
    editor = _open_long_line_file(page, seeded_session)
    assert not _tail_fits_in_editor(editor), "the long line should run past the pane by default"

    editor.locator(".view-line", has_text="def handle").click()
    page.keyboard.press("End")
    page.keyboard.type(f" // {_EDIT_MARKER}")
    # The mock rejects writes, so the typed text exists only in this editor instance.
    expect(page.get_by_text("Save failed", exact=True)).to_be_visible(timeout=15_000)
    expect(editor.locator(".selected-text")).to_have_count(0)
    page.keyboard.press("Shift+Home")
    expect(editor.locator(".selected-text").first).to_be_attached()
    editor.evaluate("el => window.wrapLinesEditor = el")

    _toggle_wrap_lines(page)
    poll_until(lambda: _tail_fits_in_editor(editor), timeout_s=10)
    expect(editor.locator(".view-line", has_text=_TAIL_MARKER)).to_be_visible()
    assert editor.evaluate("el => el === window.wrapLinesEditor"), "editor was remounted"
    expect(editor.locator(".view-line", has_text=_EDIT_MARKER)).to_be_visible()
    expect(editor.locator(".selected-text").first).to_be_attached()

    _toggle_wrap_lines(page)
    poll_until(lambda: not _tail_fits_in_editor(editor), timeout_s=10)


def test_wrap_lines_choice_survives_a_reload(page: Page, seeded_session: BrowserSession) -> None:
    """The wrap preference is restored when the page is reloaded."""
    editor = _open_long_line_file(page, seeded_session)
    _toggle_wrap_lines(page)
    poll_until(lambda: _tail_fits_in_editor(editor), timeout_s=10)

    page.reload()
    expect(editor.locator(".view-line", has_text="def handle")).to_be_visible(timeout=30_000)
    poll_until(lambda: _tail_fits_in_editor(editor), timeout_s=10)
    expect(editor.locator(".view-line", has_text=_TAIL_MARKER)).to_be_visible()
