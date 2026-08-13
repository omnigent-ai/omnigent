"""E2E: hover-revealed timestamps on chat message bubbles.

Chat bubbles show their send/receive time on hover next to the Copy/Fork
controls, without taking permanent vertical space. This drives the full
browser → SPA → server stack: send a message, wait for the mock-LLM reply,
and assert for both the user and the assistant bubble that

  - the timestamp rides inside the existing 24px action row (no new row),
  - it matches a locale time format (``h:MM AM`` style),
  - the action row is fully transparent at rest on desktop and reaches full
    opacity on hover (the hover-reveal contract),
  - the ordering matches the design target (user: timestamp → Copy at the
    right edge; assistant: Copy/Fork → timestamp at the left edge),
  - the stamp survives a full page reload (server-stamped path, not a
    re-stamped render time),
  - on a touch-sized viewport the row rests at 40% opacity so the actions
    stay discoverable without a hover affordance.

Selectors:
  - bubbles: ``data-testid="message-bubble"`` + ``data-role="user|assistant"``
  - timestamp: ``data-testid="message-timestamp"`` inside the action row
  - action row: the timestamp's parent div (``opacity-0``/``opacity-40`` base
    with ``md:group-hover:opacity-100`` reveal)
"""

from __future__ import annotations

import re
import time

from playwright.sync_api import Browser, Locator, Page, expect

_COMPOSER_PLACEHOLDER = "Ask the agent anything…"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_TIMESTAMP = '[data-testid="message-timestamp"]'
# en-US renders "2:55 PM"; 24-hour locales render "14:55". Both are valid —
# the assertion is about a clock-shaped value, not a specific locale.
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(\s?[AP]M)?$")


def _send(page: Page, text: str) -> None:
    """Type ``text`` into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER_PLACEHOLDER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _action_row(timestamp: Locator) -> Locator:
    """The action row is the timestamp span's parent div."""
    return timestamp.locator("..")


def _opacity(locator: Locator) -> str:
    return locator.evaluate("el => getComputedStyle(el).opacity")


def _wait_opacity(locator: Locator, value: str, timeout_s: float = 5.0) -> None:
    """Poll the computed opacity until it reaches ``value``.

    ``expect.poll`` is not available in the pinned Playwright, and the
    hover reveal is a CSS transition (no DOM event to await), so poll.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _opacity(locator) == value:
            return
        time.sleep(0.05)
    raise AssertionError(f"opacity never became {value!r} within {timeout_s}s")


def test_hover_reveals_timestamp_on_user_and_assistant_bubbles(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Both bubbles reveal a correctly ordered timestamp row on hover."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, "Timestamp probe: say anything.")

    # --- user bubble ---
    user_bubble = page.locator(_USER_BUBBLE).first
    expect(user_bubble).to_be_visible(timeout=15_000)
    user_ts = user_bubble.locator(_TIMESTAMP)
    expect(user_ts).to_have_count(1)
    expect(user_ts).to_have_text(_TIME_RE)
    user_row = _action_row(user_ts)

    # At rest on a desktop viewport the row is fully transparent — the
    # timestamp must not permanently occupy visual space.
    assert _opacity(user_row) == "0"
    # The row exists at the design's 24px height even while hidden.
    assert round(user_row.bounding_box()["height"]) == 24

    user_bubble.hover()
    _wait_opacity(user_row, "1")

    # Design order: timestamp → Copy at the bubble's right edge.
    copy_button = user_bubble.get_by_role("button", name="Copy")
    assert user_ts.bounding_box()["x"] < copy_button.bounding_box()["x"]

    # --- assistant bubble ---
    assistant_bubble = page.locator(_ASSISTANT_BUBBLE).first
    expect(assistant_bubble).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    assistant_ts = assistant_bubble.locator(_TIMESTAMP)
    expect(assistant_ts).to_have_count(1)
    expect(assistant_ts).to_have_text(_TIME_RE)
    assistant_row = _action_row(assistant_ts)

    assert _opacity(assistant_row) == "0"
    assert round(assistant_row.bounding_box()["height"]) == 24

    assistant_bubble.hover()
    _wait_opacity(assistant_row, "1")

    # Design order: Copy/Fork → timestamp at the bubble's left edge.
    assistant_copy = assistant_bubble.get_by_role("button", name="Copy")
    assert assistant_copy.bounding_box()["x"] < assistant_ts.bounding_box()["x"]

    # --- persistence: reload must show the same server-stamped values ---
    user_stamp = user_ts.inner_text()
    assistant_stamp = assistant_ts.inner_text()
    page.reload()
    expect(page.locator(_USER_BUBBLE).first.locator(_TIMESTAMP)).to_have_text(
        user_stamp, timeout=30_000
    )
    expect(page.locator(_ASSISTANT_BUBBLE).first.locator(_TIMESTAMP)).to_have_text(
        assistant_stamp, timeout=30_000
    )


def test_touch_viewport_keeps_timestamp_row_discoverable(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Below the md breakpoint the action row rests at 40% opacity.

    Touch devices have no hover; a permanently invisible row would hide the
    Copy action and the timestamp entirely. A failure means the
    ``opacity-40`` touch fallback regressed.
    """
    base_url, session_id = seeded_session
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, has_touch=True)
    try:
        page = ctx.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        _send(page, "Touch timestamp probe: say anything.")
        user_bubble = page.locator(_USER_BUBBLE).first
        expect(user_bubble).to_be_visible(timeout=15_000)

        user_ts = user_bubble.locator(_TIMESTAMP)
        expect(user_ts).to_have_count(1)
        expect(user_ts).to_have_text(_TIME_RE)

        # No hover performed: the row must still be partially visible.
        assert _opacity(_action_row(user_ts)) == "0.4"

        assistant_bubble = page.locator(_ASSISTANT_BUBBLE).first
        expect(assistant_bubble).to_be_visible(timeout=60_000)
        expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
        assistant_ts = assistant_bubble.locator(_TIMESTAMP)
        expect(assistant_ts).to_have_count(1)
        expect(assistant_ts).to_have_text(_TIME_RE)
        assert _opacity(_action_row(assistant_ts)) == "0.4"
    finally:
        ctx.close()
