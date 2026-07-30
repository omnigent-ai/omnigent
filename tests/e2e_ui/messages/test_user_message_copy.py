"""E2E: the copy action on a user's own message bubble.

Users can copy assistant responses; the same affordance now sits below their
own messages. This drives the full browser → SPA → server stack: send a
message, click the Copy control beneath the user bubble, and assert the text
landed on the clipboard and the icon flipped to its copied state.

Selectors:
  - user bubble: ``data-testid="message-bubble"`` + ``data-role="user"``
  - copy button: accessible name "Copy" (MessageAction sr-only label/tooltip)
  - copied state: lucide check icon (``svg.lucide-check``) replaces the copy
    icon (``svg.lucide-copy``) for ~2s after a successful write
  - composer: placeholder "Ask the agent anything…"
"""

from __future__ import annotations

import uuid

from playwright.sync_api import Browser, Page, expect

_COMPOSER_PLACEHOLDER = "Ask the agent anything…"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'


def _send(page: Page, text: str) -> None:
    """Type ``text`` into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER_PLACEHOLDER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def test_user_message_copy_button_copies_text(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Clicking Copy under a user bubble writes its text to the clipboard.

    A failure means the copy affordance regressed: either the button is not
    rendered below the user bubble, its click handler no longer writes to
    ``navigator.clipboard``, or the copied-state icon swap broke.

    Clipboard read/write requires the ``clipboard-read``/``clipboard-write``
    permissions, granted on a dedicated context here so the default
    function-scoped ``page`` fixture stays untouched.
    """
    base_url, session_id = seeded_session
    marker = f"copy-me-{uuid.uuid4().hex[:8]}"

    ctx = browser.new_context()
    ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    try:
        page = ctx.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        _send(page, marker)

        bubble = page.locator(_USER_BUBBLE).filter(has_text=marker)
        expect(bubble).to_be_visible(timeout=15_000)

        copy_button = bubble.get_by_role("button", name="Copy")
        # Copy icon is present before the click; hover-reveal only affects
        # opacity, not DOM presence, so the button is always in the tree.
        expect(copy_button.locator("svg.lucide-copy")).to_have_count(1)

        copy_button.click()

        # The write is async; the icon swaps to a check on success.
        expect(copy_button.locator("svg.lucide-check")).to_have_count(1, timeout=5_000)

        clipboard_text = page.evaluate("() => navigator.clipboard.readText()")
        assert clipboard_text == marker
    finally:
        ctx.close()
