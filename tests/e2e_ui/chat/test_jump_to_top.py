"""UI journey: the "Jump to top" affordance returns to the first message.

A long assistant reply makes the conversation overflow the viewport; after
scrolling to the bottom, hovering the conversation's top edge reveals a
"Jump to top" pill. Clicking it scrolls the view back to the very first
message (the affordance also pages in older history first, but a single
loaded window is enough to prove the scroll-to-top behavior here).

"""

from __future__ import annotations

import uuid

from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'
_WORKING = '[data-testid="working-indicator"]'
_PILL = "button[aria-label='Jump to the first message']"

# Tags the scrollable StickToBottom container so the test can read scrollTop
# and anchor the hover. The conversation viewport is the tallest scrollable
# descendant of the role="log" region.
_TAG_SCROLLER = """
() => {
  const log = document.querySelector('[role="log"]');
  let best = null;
  log.querySelectorAll('*').forEach((el) => {
    if (el.scrollHeight > el.clientHeight + 4) {
      if (!best || el.scrollHeight > best.scrollHeight) best = el;
    }
  });
  const el = best || log;
  el.setAttribute('data-pw-scroller', '1');
  el.scrollTop = el.scrollHeight;
  return el.scrollTop;
}
"""
_SCROLL_TOP = "document.querySelector('[data-pw-scroller]').scrollTop"


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def test_jump_to_top_returns_to_first_message(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    first_token = f"first-{uuid.uuid4().hex[:8]}"
    page.goto(f"{base_url}/c/{session_id}")

    # Turn 1: a recognizable FIRST message — this is the jump target.
    _send(page, f"Reply with exactly this token and nothing else: {first_token}")
    expect(page.locator(_ASSISTANT, has_text=first_token).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # Turn 2: a long reply so the conversation overflows the viewport and
    # becomes scrollable — otherwise there's no "up" to jump to.
    _send(page, "Reply with the numbers 1 through 100, each on its own line, and nothing else.")
    expect(page.locator(_USER)).to_have_count(2, timeout=15_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=90_000)

    # Scroll to the bottom so the first message is well out of view.
    scroll_top = page.evaluate(_TAG_SCROLLER)
    assert scroll_top > 100, (
        f"conversation did not overflow enough to scroll (scrollTop={scroll_top})"
    )
    # The first message is no longer in the viewport.
    expect(page.locator(_USER).first).not_to_be_in_viewport()

    # Hover the top edge to reveal the pill, then click it. The hover is
    # detected on the conversation wrapper, so moving the cursor onto the pill
    # (which Playwright does on click) keeps it revealed and clickable.
    box = page.locator("[data-pw-scroller]").bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 40)
    page.locator(_PILL).click()

    # It lands at the very top: the first message is back in view and the
    # scroll position has settled at (or within a pixel of) the top.
    expect(page.locator(_USER).first).to_be_in_viewport(timeout=30_000)
    page.wait_for_function(f"{_SCROLL_TOP} <= 2", timeout=30_000)
