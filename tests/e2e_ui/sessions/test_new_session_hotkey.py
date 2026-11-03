"""E2E: Cmd/Ctrl+Shift+O opens a new session from focused chat input."""

from __future__ import annotations

import sys

from playwright.sync_api import Page, expect

_COMPOSER = "Send a message…"


def test_new_session_hotkey_from_focused_composer(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The browser shortcut opens a clean composer and preserves the old draft."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("draft that belongs to the existing session")
    expect(composer).to_be_focused()

    modifier = "Meta" if sys.platform == "darwin" else "Control"
    page.keyboard.press(f"{modifier}+Shift+o")

    expect(page).to_have_url(f"{base_url}/", timeout=10_000)
    new_session_composer = page.get_by_placeholder("Describe a task to start a new session…")
    expect(new_session_composer).to_be_visible()
    expect(new_session_composer).to_be_focused()
    expect(new_session_composer).to_have_value("")

    page.go_back()
    expect(composer).to_have_value("draft that belongs to the existing session")
