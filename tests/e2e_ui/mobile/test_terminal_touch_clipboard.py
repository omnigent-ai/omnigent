"""E2E: copy terminal output and paste into a terminal on a phone.

xterm.js draws output on a canvas and reads input from a hidden textarea, so
touch browsers (the iOS app's WKWebView included) offer no long-press
selection, Copy menu, or paste target. On touch devices the terminal pane
shows a clipboard bar instead: Copy opens a sheet of the terminal text with
native selection, and Paste reads the clipboard and sends it as input.

Both tests run in a phone-profile Chromium context (390x844, ``has_touch``)
created after the session fixture, with clipboard permissions granted so the
browser clipboard can be checked directly.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from playwright.sync_api import Browser, Locator, Page, ViewportSize, expect

from tests._helpers.browser import user_page

_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}
_RECORD_DIR = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
# Recorded runs hold each state long enough to read; plain runs don't wait.
_READ_PAUSE_MS = 2_000 if _RECORD_DIR else 0


@contextmanager
def _phone_page(browser: Browser, base_url: str) -> Iterator[Page]:
    """Open a touch phone page with clipboard access, recording when requested."""
    recording = {"record_video_dir": _RECORD_DIR} if _RECORD_DIR else {}
    with user_page(
        browser,
        headers={},
        viewport=_MOBILE_VIEWPORT,
        has_touch=True,
        is_mobile=True,
        **recording,
    ) as page:
        page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
        yield page


def _pause(page: Page) -> None:
    if _READ_PAUSE_MS:
        page.wait_for_timeout(_READ_PAUSE_MS)


def _open_new_shell(page: Page, base_url: str, session_id: str) -> Locator:
    """Open a shell the way a phone user does and wait for it to connect."""
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_role("button", name="Conversation actions").click()
    shells_entry = page.get_by_role("menuitem", name="Shells", exact=True)
    expect(shells_entry).to_be_visible(timeout=10_000)
    shells_entry.click()
    drawer = page.get_by_test_id("shells-panel-drawer")
    expect(drawer).to_have_attribute("data-state", "open")
    drawer.get_by_role("button", name="New shell").click()

    # A hidden pre-warmed agent pane also carries the testid.
    terminal_view = page.locator('[data-testid="terminal-view"]:visible').first
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=30_000)
    return terminal_view


def _focus_terminal(terminal_view: Locator) -> None:
    terminal_view.locator("textarea.xterm-helper-textarea").focus()


def _open_copy_sheet(terminal_view: Locator, page: Page) -> Locator:
    """Tap Copy on the clipboard bar and return the sheet."""
    terminal_view.get_by_test_id("terminal-touch-clipboard").get_by_role(
        "button", name="Copy", exact=True
    ).tap()
    sheet = page.get_by_test_id("terminal-copy-sheet")
    expect(sheet).to_be_visible()
    return sheet


def _close_copy_sheet(page: Page, sheet: Locator) -> None:
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()


def _open_sheet_when(
    terminal_view: Locator,
    page: Page,
    shows: Callable[[list[str]], bool],
    expected: str,
    timeout_ms: int = 15_000,
) -> Locator:
    """Reopen the copy sheet, a snapshot of the terminal, until ``shows`` its lines."""
    remaining_ms = timeout_ms
    while True:
        sheet = _open_copy_sheet(terminal_view, page)
        text = sheet.get_by_test_id("terminal-copy-text").inner_text()
        if shows(text.split("\n")):
            return sheet
        if remaining_ms <= 0:
            raise AssertionError(f"terminal never showed {expected}; it showed:\n{text}")
        _close_copy_sheet(page, sheet)
        page.wait_for_timeout(250)
        remaining_ms -= 250


def _open_sheet_showing_line(terminal_view: Locator, page: Page, line: str) -> Locator:
    return _open_sheet_when(terminal_view, page, lambda lines: line in lines, f"the line {line!r}")


def _clipboard_text(page: Page) -> str:
    return page.evaluate("() => navigator.clipboard.readText()")


def test_phone_copies_terminal_output(
    browser: Browser,
    terminal_session: tuple[str, str],
) -> None:
    """CUJ-1: select one line of output and copy it, then copy all of it."""
    base_url, session_id = terminal_session
    marker = f"copied-{uuid.uuid4().hex[:8]}"
    with _phone_page(browser, base_url) as page:
        terminal_view = _open_new_shell(page, base_url, session_id)
        _focus_terminal(terminal_view)
        page.keyboard.type(f"echo {marker}")
        page.keyboard.press("Enter")
        _pause(page)

        sheet = _open_sheet_showing_line(terminal_view, page, marker)
        sheet_text = sheet.get_by_test_id("terminal-copy-text")
        _pause(page)

        # Desktop Chromium cannot drive the iOS long-press, so place the
        # selection a long-press would make: the output line only.
        sheet_text.evaluate(
            """(pre, marker) => {
              const lines = pre.textContent.split("\\n");
              const index = lines.lastIndexOf(marker);
              const start = lines.slice(0, index).join("\\n").length + (index > 0 ? 1 : 0);
              const node = pre.firstChild;
              const range = document.createRange();
              range.setStart(node, start);
              range.setEnd(node, start + marker.length);
              const selection = window.getSelection();
              selection.removeAllRanges();
              selection.addRange(range);
            }""",
            marker,
        )
        copy_selection = sheet.get_by_role("button", name="Copy selection")
        expect(copy_selection).to_be_visible()
        _pause(page)
        copy_selection.tap()
        expect(page.get_by_text("Copied selection")).to_be_visible()
        expect(sheet).to_be_hidden()
        assert _clipboard_text(page) == marker
        _pause(page)

        sheet = _open_copy_sheet(terminal_view, page)
        sheet.get_by_role("button", name="Copy all").tap()
        expect(page.get_by_text("Copied terminal text")).to_be_visible()
        assert f"echo {marker}\n{marker}" in _clipboard_text(page)
        _pause(page)


def test_phone_pastes_into_terminal(
    browser: Browser,
    terminal_session: tuple[str, str],
) -> None:
    """CUJ-2: paste clipboard text at the prompt and run it."""
    base_url, session_id = terminal_session
    command = f"echo pasted-{uuid.uuid4().hex[:8]}-$((6*7))"
    with _phone_page(browser, base_url) as page:
        terminal_view = _open_new_shell(page, base_url, session_id)
        _focus_terminal(terminal_view)
        # Stands in for copying the command in another app.
        page.evaluate("(text) => navigator.clipboard.writeText(text)", command)
        _pause(page)

        paste = terminal_view.get_by_test_id("terminal-touch-clipboard").get_by_role(
            "button", name="Paste", exact=True
        )
        expect(paste).to_be_enabled()
        paste.tap()
        # The pasted command waits at the prompt, not yet run.
        sheet = _open_sheet_when(
            terminal_view,
            page,
            lambda lines: bool(lines) and lines[-1].endswith(command),
            f"{command!r} at the prompt",
        )
        _pause(page)
        _close_copy_sheet(page, sheet)
        _pause(page)

        _focus_terminal(terminal_view)
        page.keyboard.press("Enter")
        output = command.replace("echo ", "").replace("$((6*7))", "42")
        sheet = _open_sheet_showing_line(terminal_view, page, output)
        _pause(page)
        _close_copy_sheet(page, sheet)
