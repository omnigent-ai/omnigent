"""E2E: HTML angle brackets typed into chat survive in the web user bubble.

Text like ``<div>`` typed into the chat composer must stay visible in the
user's message bubble in the web UI. The user bubble renders through the
markdown pipeline, whose raw-HTML handling would otherwise drop
angle-bracketed tokens. The stored transcript keeps the full text (which is
why the TUI renders it verbatim), so any missing token is purely a
display-side defect in the web SPA.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
from playwright.sync_api import Page, expect

_COMPOSER_LABEL = "Message the agent"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'

# Prose a user plausibly types when discussing markup: every angle-bracketed
# token must stay visible verbatim in the rendered bubble.
_TAG_TOKENS = ("<div>", "<section>", '<span class="note">')
_MESSAGE_TEXT = (
    'Please change the <div> wrapper to <section> and keep the <span class="note"> tag as-is.'
)


def _strings_in(value: object) -> list[str]:
    """Flatten every string found anywhere in a decoded JSON *value*."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for child in value.values() for s in _strings_in(child)]
    if isinstance(value, list):
        return [s for child in value for s in _strings_in(child)]
    return []


def _stored_items_contain(base_url: str, session_id: str, text: str) -> bool:
    """Whether *text* appears verbatim in the session's stored transcript."""
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000},
        timeout=10.0,
    )
    response.raise_for_status()
    return any(text in s for s in _strings_in(response.json()))


def _wait_until(page: Page, predicate: Callable[[], bool], *, timeout_s: float = 30.0) -> None:
    """Poll *predicate* until true, pumping the page event loop between polls."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(200)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def test_user_message_keeps_html_angle_brackets(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Angle-bracketed tags typed by the user stay visible in their bubble."""
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill(_MESSAGE_TEXT)
    page.get_by_role("button", name="Send", exact=True).click()

    bubble = page.locator(_USER_BUBBLE).last
    expect(bubble).to_be_visible(timeout=30_000)
    # Anchor on the prose around the tags so the bubble is fully painted
    # before the tag tokens are judged.
    expect(bubble).to_contain_text("Please change the", timeout=30_000)
    expect(bubble).to_contain_text("tag as-is.", timeout=30_000)

    # The transcript stores the message verbatim (the TUI renders this same
    # stored text correctly), so a tag missing from the bubble below is a
    # web-display bug, not a data-loss bug.
    _wait_until(
        page,
        lambda: _stored_items_contain(base_url, session_id, _MESSAGE_TEXT),
    )

    # The user-visible failure: the angle-bracketed tokens vanish from the
    # rendered bubble. The bubble is already painted (prose anchors above),
    # so a short timeout suffices for each token.
    for token in _TAG_TOKENS:
        expect(bubble).to_contain_text(token, timeout=5_000)
