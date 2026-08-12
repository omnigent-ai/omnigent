"""UI e2e: the command palette leaves the caret in the destination composer.

This one only reproduces in a real browser. The palette is a modal dialog, so
Radix traps focus inside it and only releases the trap when the close animation
ends — several frames after the selection navigated away. A destination that
focuses its composer as it mounts therefore has that focus yanked back into the
dying dialog and dropped on ``<body>``. jsdom runs no animations, so a unit test
sees the trap release immediately and never catches it.

The journey under test opens the palette, picks a destination, then types the
next prompt without clicking the composer — the caret has to already be there.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _create_runner_bound_session

# The new-session landing screen's composer.
_LANDING_INPUT = "new-chat-landing-input"
# The in-session composer, which carries no test id — match its placeholder,
# as the other composer e2e tests do.
_SESSION_COMPOSER = "Ask the agent anything…"

# cmdk's data-value provides stable action/session identity even when a shared
# server has duplicate labels or highlights a matching session.
_NEW_CHAT_ITEM = '[cmdk-item][data-value="action:new-chat"]'

# Identifies whatever holds the caret: the landing composer's test id, or the
# in-session composer's placeholder.
_ACTIVE_COMPOSER_JS = """
() => {
  const el = document.activeElement;
  if (!el || el.tagName !== 'TEXTAREA') return null;
  return el.getAttribute('data-testid') ?? el.getAttribute('placeholder');
}
"""


def _wait_for_caret(page: Page, expected: str) -> None:
    """Wait for `expected` to hold the caret.

    Waiting rather than asserting immediately is what catches the bug: the
    destination focuses itself on mount and the dying palette steals it back a
    beat later, so an immediate assertion would pass on the broken build.
    """
    page.wait_for_function(f"() => ({_ACTIVE_COMPOSER_JS})() === {expected!r}", timeout=10_000)


def test_new_chat_from_palette_focuses_the_landing_composer(
    page: Page,
    seeded_session: tuple[str, str],
    runner_id: str,
) -> None:
    base_url, session_id = seeded_session
    # A matching session can steal cmdk's highlight from the New chat action.
    # Selecting by data-value still commits the intended action.
    decoy_id = _create_runner_bound_session(base_url, runner_id)
    httpx.patch(
        f"{base_url}/v1/sessions/{decoy_id}",
        json={"title": "new chat decoy"},
        timeout=10.0,
    ).raise_for_status()
    try:
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.get_by_placeholder(_SESSION_COMPOSER)).to_be_visible(timeout=30_000)

        page.keyboard.press("Control+k")
        palette = page.get_by_test_id("command-palette-input")
        expect(palette).to_be_visible(timeout=10_000)
        # Narrow the list, then commit by identity instead of cmdk's highlight
        # after the debounced session search reshuffle.
        palette.type("new chat", delay=20)
        new_chat = page.locator(_NEW_CHAT_ITEM)
        expect(new_chat).to_be_visible(timeout=10_000)
        new_chat.click()

        expect(page.get_by_test_id(_LANDING_INPUT)).to_be_visible(timeout=15_000)
        _wait_for_caret(page, _LANDING_INPUT)

        # The whole point: type the next prompt with no click in between.
        page.keyboard.type("ship the thing")
        expect(page.get_by_test_id(_LANDING_INPUT)).to_have_value("ship the thing")
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{decoy_id}", timeout=10.0)


def test_sidebar_new_session_focuses_the_landing_composer(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The sidebar link has no overlay in the way, but the caret must land the
    same place — this is the other route onto the new-session page."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_SESSION_COMPOSER)).to_be_visible(timeout=30_000)

    page.get_by_test_id("new-chat-button").click()

    expect(page.get_by_test_id(_LANDING_INPUT)).to_be_visible(timeout=15_000)
    _wait_for_caret(page, _LANDING_INPUT)


def test_session_switch_from_palette_focuses_the_session_composer(
    page: Page,
    seeded_session: tuple[str, str],
    runner_id: str,
) -> None:
    base_url, session_id = seeded_session
    # A sibling untitled session proves duplicate fallback labels are harmless.
    sibling_id = _create_runner_bound_session(base_url, runner_id)
    try:
        page.goto(base_url)
        expect(page.get_by_test_id(_LANDING_INPUT)).to_be_visible(timeout=30_000)

        # Click the session row by identity; this test concerns focus after the
        # palette closes, not cmdk's transient highlight while results load.
        page.keyboard.press("Control+k")
        expect(page.get_by_test_id("command-palette-input")).to_be_visible(timeout=10_000)
        # Identity, not the "New session" fallback label.
        session_row = page.locator(f'[cmdk-item][data-value="{session_id}"]')
        expect(session_row).to_be_visible(timeout=15_000)
        session_row.click()

        page.wait_for_url(f"**/c/{session_id}", timeout=15_000)
        _wait_for_caret(page, _SESSION_COMPOSER)
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{sibling_id}", timeout=10.0)
