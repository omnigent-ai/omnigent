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

from playwright.sync_api import Browser, BrowserContext, Locator, Page, ViewportSize, expect

_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# Short holds so a recorded run shows each state long enough to read.
_READ_PAUSE_MS = 2_000


def _phone_context(browser: Browser, base_url: str) -> BrowserContext:
    """Open a touch phone context, recording when the run requests it."""
    context_kwargs: dict[str, object] = {
        "viewport": _MOBILE_VIEWPORT,
        "has_touch": True,
        "is_mobile": True,
    }
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        context_kwargs["record_video_dir"] = record_dir
    context = browser.new_context(**context_kwargs)
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
    return context


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


def _run_command(page: Page, terminal_view: Locator, command: str) -> None:
    """Type a command into the terminal and run it."""
    terminal_view.locator("textarea.xterm-helper-textarea").focus()
    page.keyboard.type(command)
    page.keyboard.press("Enter")


def _open_copy_sheet(terminal_view: Locator, page: Page) -> Locator:
    """Tap Copy on the clipboard bar and return the sheet."""
    terminal_view.get_by_test_id("terminal-touch-clipboard").get_by_role(
        "button", name="Copy", exact=True
    ).tap()
    sheet = page.get_by_test_id("terminal-copy-sheet")
    expect(sheet).to_be_visible()
    return sheet


def _clipboard_text(page: Page) -> str:
    return page.evaluate("() => navigator.clipboard.readText()")


def test_phone_copies_terminal_output(
    browser: Browser,
    terminal_session: tuple[str, str],
) -> None:
    """CUJ-1: select one line of output and copy it, then copy all of it."""
    base_url, session_id = terminal_session
    marker = f"copied-{uuid.uuid4().hex[:8]}"
    context = _phone_context(browser, base_url)
    try:
        page = context.new_page()
        terminal_view = _open_new_shell(page, base_url, session_id)
        _run_command(page, terminal_view, f"echo {marker}")
        page.wait_for_timeout(_READ_PAUSE_MS)

        sheet = _open_copy_sheet(terminal_view, page)
        sheet_text = sheet.get_by_test_id("terminal-copy-text")
        expect(sheet_text).to_contain_text(f"echo {marker}")
        page.wait_for_timeout(_READ_PAUSE_MS)

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
        page.wait_for_timeout(_READ_PAUSE_MS)
        copy_selection.tap()
        expect(page.get_by_text("Copied selection")).to_be_visible()
        expect(sheet).to_be_hidden()
        assert _clipboard_text(page) == marker
        page.wait_for_timeout(_READ_PAUSE_MS)

        sheet = _open_copy_sheet(terminal_view, page)
        sheet.get_by_role("button", name="Copy all").tap()
        expect(page.get_by_text("Copied terminal text")).to_be_visible()
        copied = _clipboard_text(page)
        assert f"echo {marker}\n{marker}" in copied
        page.wait_for_timeout(_READ_PAUSE_MS)
    finally:
        context.close()


def test_phone_pastes_into_terminal(
    browser: Browser,
    terminal_session: tuple[str, str],
) -> None:
    """CUJ-2: paste clipboard text at the prompt and run it."""
    base_url, session_id = terminal_session
    context = _phone_context(browser, base_url)
    try:
        page = context.new_page()
        terminal_view = _open_new_shell(page, base_url, session_id)
        terminal_view.locator("textarea.xterm-helper-textarea").focus()
        # Stands in for copying the command in another app.
        page.evaluate("(text) => navigator.clipboard.writeText(text)", "echo pasted-$((6*7))")
        page.wait_for_timeout(_READ_PAUSE_MS)

        paste = terminal_view.get_by_test_id("terminal-touch-clipboard").get_by_role(
            "button", name="Paste", exact=True
        )
        expect(paste).to_be_enabled()
        paste.tap()
        page.wait_for_timeout(_READ_PAUSE_MS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(_READ_PAUSE_MS)

        sheet = _open_copy_sheet(terminal_view, page)
        sheet_text = sheet.get_by_test_id("terminal-copy-text")
        expect(sheet_text).to_contain_text("echo pasted-$((6*7))")
        expect(sheet_text).to_contain_text("\npasted-42")
        page.wait_for_timeout(_READ_PAUSE_MS)
    finally:
        context.close()
