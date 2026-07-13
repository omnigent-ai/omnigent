"""UI journey: the "Jump to top" affordance returns to the first message.

A few short turns in a deliberately short viewport make the conversation
overflow; after scrolling to the bottom, hovering the conversation's top edge
reveals a "Jump to top" pill. Clicking it scrolls the view back to the very
first message (the affordance also pages in older history first, but a single
loaded window is enough to prove the scroll-to-top behavior here).

Overflow is forced by the small viewport + turn *count*, never by the length
of any model reply — asserting on what the LLM says (a tall numbered list, an
exact echoed token) proved flaky in CI.

"""

from __future__ import annotations

from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'
_WORKING = '[data-testid="working-indicator"]'
_PILL = "button[aria-label='Jump to the first message']"

# A short viewport so a handful of short turns overflow the conversation
# deterministically — see module docstring.
_VIEWPORT = {"width": 1280, "height": 320}

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
# True while the pill is revealed and clickable (opacity-100 / pointer-events-auto).
_PILL_INTERACTIVE = (
    "document.querySelector(\"button[aria-label='Jump to the first message']\")"
    ".className.includes('pointer-events-auto')"
)


def _send_turn(page: Page, text: str, turn: int) -> None:
    """Send *text* and wait for the *turn*-th round trip to fully render.

    Waits on bubble counts (one user + one assistant bubble per turn) and the
    working indicator clearing, so the next turn isn't sent mid-stream — no
    dependence on the reply's text or length.
    """
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_USER)).to_have_count(turn, timeout=15_000)
    expect(page.locator(_ASSISTANT)).to_have_count(turn, timeout=90_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=90_000)


def test_jump_to_top_returns_to_first_message(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    page.set_viewport_size(_VIEWPORT)

    # Three short turns. The first user bubble is the jump target; the rest
    # exist only to push it out of view in the short viewport.
    for turn, prompt in enumerate(("Say hello.", "Say hello again.", "Say hello once more."), 1):
        _send_turn(page, prompt, turn)

    # Scroll to the bottom so the first message is out of view.
    scroll_top = page.evaluate(_TAG_SCROLLER)
    assert scroll_top > 50, (
        f"conversation did not overflow enough to scroll (scrollTop={scroll_top})"
    )
    expect(page.locator(_USER).first).not_to_be_in_viewport()

    # Hover the top of the conversation to reveal the pill, then click it. The
    # hover is detected on the conversation wrapper, so moving the cursor onto
    # the pill (which Playwright does on click) keeps it revealed and clickable.
    # The hover point must clear the ~56px ChatHeader overlay — a separate DOM
    # subtree whose box would otherwise swallow the mousemove — while staying
    # inside the pill's reveal band (140px from the top).
    box = page.locator("[data-pw-scroller]").bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 90)
    page.locator(_PILL).click()

    # It lands at the very top: the first message is back in view and the
    # scroll position has settled at (or within a pixel of) the top.
    expect(page.locator(_USER).first).to_be_in_viewport(timeout=30_000)
    page.wait_for_function(f"{_SCROLL_TOP} <= 2", timeout=30_000)


def test_jump_to_top_reveals_on_scroll_up_then_auto_hides(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Scrolling up surfaces the pill (no hover needed); pausing fades it out."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    page.set_viewport_size(_VIEWPORT)

    for turn, prompt in enumerate(("Say hello.", "Say hello again.", "Say hello once more."), 1):
        _send_turn(page, prompt, turn)

    # Scroll to the bottom: the pill is hidden there.
    scroll_top = page.evaluate(_TAG_SCROLLER)
    assert scroll_top > 50, (
        f"conversation did not overflow enough to scroll (scrollTop={scroll_top})"
    )
    assert page.evaluate(_PILL_INTERACTIVE) is False

    # Park the cursor near the BOTTOM of the conversation — clear of the 140px
    # top hover band — so the reveal (and, crucially, the auto-hide) is driven by
    # the scroll alone, never a lingering hover. The viewport is only 320px tall,
    # so the conversation's *center* still falls inside the band; the bottom edge
    # does not.
    box = page.locator("[data-pw-scroller]").bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 5)

    # Scroll up to roughly the middle. Setting scrollTop dispatches a real
    # 'scroll' event — the same signal a wheel/drag emits, and exactly how
    # _TAG_SCROLLER above scrolls — so this drives the component's scroll
    # direction detection deterministically, without depending on wheel-delta
    # mapping. Staying near the middle keeps us clear of the very top, where the
    # at-top state deliberately suppresses the scroll reveal.
    mid = page.evaluate(
        "() => { const el = document.querySelector('[data-pw-scroller]');"
        " el.scrollTop = Math.floor(el.scrollHeight / 2); return el.scrollTop; }"
    )
    assert mid > 1, f"scroll-up did not land clear of the top (scrollTop={mid})"

    # The pill reveals on the upward scroll…
    page.wait_for_function(_PILL_INTERACTIVE, timeout=10_000)
    # …then fades back out once scrolling settles (SCROLL_REVEAL_MS = 2000ms).
    page.wait_for_function(f"!({_PILL_INTERACTIVE})", timeout=10_000)
