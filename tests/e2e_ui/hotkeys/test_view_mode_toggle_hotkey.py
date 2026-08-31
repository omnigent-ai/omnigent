"""Assembled UI: Ctrl+Alt+T toggles Chat/Terminal from both focus surfaces.

The terminal-first mock-Claude fixture supplies a real connected xterm without
live model traffic. The test leaves an unsent composer draft, enters Terminal
from that focused editor, then returns while xterm's helper textarea owns
focus. The unchanged draft proves neither chord submitted or inserted text.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

_CHORD = "Control+Alt+KeyT"
_COMPOSER = "Ask the agent anything…"
_DRAFT = "keep this draft unsent while toggling views"
_TERMINAL_VIEW = '[data-testid="terminal-view"]'
_XTERM_INPUT = ".xterm-helper-textarea"
_READY_TIMEOUT_MS = 120_000


@pytest.mark.timeout(180)
def test_view_mode_hotkey_from_composer_and_focused_terminal(
    page: Page,
    native_claude_mock_session: tuple[str, str],
) -> None:
    """Chat → Terminal from composer, then Terminal → Chat from focused xterm."""
    base_url, session_id = native_claude_mock_session
    page.goto(f"{base_url}/c/{session_id}")

    toggle = page.get_by_test_id("view-mode-toggle")
    expect(toggle).to_be_visible(timeout=_READY_TIMEOUT_MS)

    # Native sessions may restore Terminal as their last/default view. Establish
    # Chat before testing the keyboard-only round trip.
    chat_button = page.get_by_test_id("view-mode-chat")
    if chat_button.get_attribute("aria-pressed") != "true":
        chat_button.click()

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(_DRAFT)
    expect(composer).to_be_focused()

    # The app-level chord must win while the editable composer owns focus.
    page.keyboard.press(_CHORD)
    visible_terminal = page.locator('[data-testid="main-terminal-view"][data-visible="true"]')
    expect(visible_terminal).to_be_visible(timeout=30_000)

    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute("data-state", "connected", timeout=_READY_TIMEOUT_MS)
    xterm_input = terminal.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached()
    xterm_input.focus()
    expect(xterm_input).to_be_focused()

    # Deliberately unlike ⌘K, the same chord is claimed inside xterm so the
    # user can leave Terminal without typing the chord into the PTY.
    page.keyboard.press(_CHORD)
    expect(composer).to_be_visible(timeout=30_000)
    expect(composer).to_be_focused()
    expect(composer).to_have_value(_DRAFT)
    expect(
        page.locator('[data-testid="message-bubble"][data-role="user"]').filter(has_text=_DRAFT)
    ).to_have_count(0)

    # The command-palette action uses the same guarded setView transition.
    page.keyboard.press("Control+k")
    palette = page.get_by_role("dialog")
    expect(palette).to_be_visible()
    palette.get_by_text("Toggle chat / terminal view", exact=True).click()
    expect(visible_terminal).to_be_visible(timeout=30_000)
    page.get_by_test_id("view-mode-chat").click()
    expect(composer).to_be_visible()
