"""E2E: the terminal view's scrollback is unreachable by touch on mobile.

Regression test: on a phone, touch-dragging in a terminal pane must move
the scrollback — without a touch input path the view stays pinned to the
live bottom, so output that has scrolled off is unreachable. Mouse-wheel
scrolling in the same pane works on desktop, so a failure here is
specifically the touch input path, not missing scrollback data.

Journey (mirrors the report's steps, driven with real browser touch input):

1. On a phone-profile browser (mobile viewport, ``has_touch``), open a
   session whose agent declares shell access.
2. Open a terminal pane (kebab → Shells → "New shell" — the mobile create
   entry point) and run a command that prints more lines than fit on one
   screen, filling xterm's scrollback.
3. Sanity: wheel scrolling moves the scrollback (proves the buffer is there
   and scrollable — the desktop path that works).
4. Finger-swipe downward on the terminal output (the "scroll back" gesture)
   using CDP ``Input.dispatchTouchEvent``, which goes through the browser's
   real input pipeline — native touch scrolling and JS touch handlers alike
   would both move the view.
5. Assert the scroll position moved back toward older output.

Observable: xterm v6 renders scroll state through a VS Code-style custom
``.xterm-scrollable-element`` — its vertical scrollbar ``.slider``'s ``top``
tracks the viewport position (the native ``.xterm-viewport`` element's
``scrollTop`` stays 0). "Pinned to the live bottom" = slider at its maximum
top; "scrolled back" = slider top decreased.

On the unfixed build step 5 fails: ``TerminalSession.ts`` wires only
``attachCustomWheelEventHandler`` (wheel events), no touch handler exists,
and the xterm mount sits under ``overflow-hidden`` wrappers with the
``.xterm-screen`` hit target being a *sibling* of the scroll machinery — so
a finger drag has nowhere to go and the slider never moves. After a fix (a
touch handler driving ``term.scrollLines()``, or CSS routing touch pans into
the scrollable element) the swipe moves the view and the test passes.
"""

from __future__ import annotations

import os

from playwright.sync_api import Browser, Page, ViewportSize, expect

# iPhone-12-class portrait viewport — below the Tailwind ``md`` breakpoint,
# so the mobile kebab navigation (the phone user's entry point) renders.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# More lines than a phone-height terminal shows at once (~40 rows at this
# viewport), so the scrollback buffer is guaranteed non-empty.
_SCROLLBACK_LINES = 200

# Scroll state of the visible terminal pane. xterm v6's scroll position is
# NOT the native viewport scrollTop; read the custom scrollable element's
# vertical slider geometry instead. ``sliderTop`` grows as the view nears
# the live bottom; ``maxTop = trackHeight - sliderHeight`` is "pinned".
_SCROLL_STATE = """
() => {
  const views = Array.from(
    document.querySelectorAll('[data-testid="terminal-view"]'),
  ).filter((el) => el.offsetParent !== null);
  if (!views.length) return null;
  const el = views[0];
  const track = el.querySelector(
    '.xterm-scrollable-element .scrollbar.vertical',
  );
  const slider = track ? track.querySelector('.slider') : null;
  if (!track || !slider) return null;
  return {
    sliderTop: parseFloat(slider.style.top || '0'),
    sliderHeight: parseFloat(slider.style.height || '0'),
    trackHeight: track.getBoundingClientRect().height,
  };
}
"""


def _scroll_state(page: Page) -> dict[str, float]:
    """Read the visible pane's scrollbar geometry, asserting it exists."""
    state = page.evaluate(_SCROLL_STATE)
    assert state is not None, "visible terminal pane / xterm scrollbar not found"
    return state


def _touch_swipe(page: Page, x: float, y_from: float, y_to: float, steps: int = 12) -> None:
    """Drive a one-finger vertical swipe through the browser's input pipeline.

    Uses CDP ``Input.dispatchTouchEvent`` (not synthetic JS ``TouchEvent``s),
    so the gesture exercises everything a real finger would: native
    touch-scrolling of scrollable elements AND any JS touch handlers. A
    downward swipe (``y_to > y_from``) is the "scroll back / reveal older
    content" gesture.

    :param page: Page whose CDP session receives the touch events.
    :param x: Horizontal position of the finger (CSS px).
    :param y_from: Finger start Y.
    :param y_to: Finger end Y.
    :param steps: Number of intermediate ``touchMove`` events.
    """
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [{"x": x, "y": y_from}]},
        )
        for i in range(1, steps + 1):
            y = y_from + (y_to - y_from) * i / steps
            cdp.send(
                "Input.dispatchTouchEvent",
                {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]},
            )
            page.wait_for_timeout(16)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        # Let any momentum / scroll animation settle before reading state.
        page.wait_for_timeout(600)
    finally:
        cdp.detach()


def test_terminal_touch_swipe_scrolls_back(
    browser: Browser,
    terminal_session: tuple[str, str],
) -> None:
    """A finger swipe on the terminal output must scroll back through scrollback.

    Fails on the unfixed build: the swipe leaves the pane pinned to the live
    bottom (no touch handler, wheel-only scrolling), which is exactly the
    reported bug. The wheel sanity step passing while the touch step fails
    pins the regression to the touch input path specifically.

    :param browser: Playwright browser; the test opens its own touch-enabled
        phone-profile context (the default ``page`` fixture has no touch).
    :param terminal_session: ``(base_url, session_id)`` of a runner-bound
        session whose agent declares a shell (``terminals:`` block).
    """
    base_url, session_id = terminal_session

    context_kwargs: dict[str, object] = {
        "viewport": _MOBILE_VIEWPORT,
        "has_touch": True,
        "is_mobile": True,
    }
    # The e2e_ui recording hook patches the async API only; opt this sync
    # context into recording explicitly when a run requests it.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        context_kwargs["record_video_dir"] = record_dir
    context = browser.new_context(**context_kwargs)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        # Step 2 — open a terminal pane the way a phone user does: the header
        # kebab lists Shells (the agent declares a ``terminals:`` block), and
        # the drawer's "New shell" row creates and opens the shell full-screen.
        page.get_by_role("button", name="Conversation actions").click()
        shells_entry = page.get_by_role("menuitem", name="Shells", exact=True)
        expect(shells_entry).to_be_visible(timeout=10_000)
        shells_entry.click()
        drawer = page.get_by_test_id("shells-panel-drawer")
        expect(drawer).to_have_attribute("data-state", "open")
        drawer.get_by_role("button", name="New shell").click()

        # The shell opens in the full-screen terminals panel; wait for the
        # xterm bridge to connect (visible pane only — a hidden pre-warmed
        # agent pane also carries the testid).
        terminal_view = page.locator('[data-testid="terminal-view"]:visible').first
        expect(terminal_view).to_be_visible(timeout=60_000)
        expect(terminal_view).to_have_attribute("data-state", "connected", timeout=30_000)

        # Fill the scrollback: type a command printing more lines than fit
        # on one phone screen. xterm's hidden helper textarea carries focus.
        terminal_view.locator("textarea.xterm-helper-textarea").focus()
        fill_cmd = f'for i in $(seq 1 {_SCROLLBACK_LINES}); do echo "scroll-line-$i"; done'
        page.keyboard.type(fill_cmd)
        page.keyboard.press("Enter")

        # Wait until xterm has accumulated overflow: the vertical slider
        # shrinks below the track and sits pinned at its max top (live view).
        deadline_ms = 30_000
        waited = 0
        while waited < deadline_ms:
            s = page.evaluate(_SCROLL_STATE)
            if s and s["sliderHeight"] > 0 and s["sliderTop"] > 50:
                break
            page.wait_for_timeout(500)
            waited += 500
        pinned = _scroll_state(page)
        assert pinned["sliderTop"] > 50, (
            f"terminal never accumulated scrollback (slider never left the top): {pinned}"
        )

        box = terminal_view.bounding_box()
        assert box is not None
        cx = box["x"] + box["width"] / 2
        # Step 3 — wheel sanity: the desktop scroll path works, proving the
        # scrollback is present and scrollable (so a touch failure below is
        # the input path, not missing data).
        page.mouse.move(cx, box["y"] + box["height"] / 2)
        page.mouse.wheel(0, -800)
        page.wait_for_timeout(600)
        after_wheel = _scroll_state(page)
        assert after_wheel["sliderTop"] < pinned["sliderTop"] - 1, (
            f"wheel scroll did not move scrollback (harness problem, not the "
            f"reported bug): pinned={pinned} after={after_wheel}"
        )
        # Re-pin to the live bottom so the touch step starts from the same
        # state the reporter did.
        page.mouse.wheel(0, 100_000)
        page.wait_for_timeout(600)
        repinned = _scroll_state(page)
        assert repinned["sliderTop"] >= pinned["sliderTop"] - 1, (
            f"could not re-pin the terminal to the live bottom: {repinned}"
        )

        # Step 4 — the reported gesture: finger drag DOWNWARD on the output
        # (the universal "scroll back / reveal older content" touch gesture).
        y_top = box["y"] + box["height"] * 0.25
        y_bottom = box["y"] + box["height"] * 0.75
        _touch_swipe(page, cx, y_from=y_top, y_to=y_bottom)

        # Step 5 — the scrollback must have moved back (slider moved up).
        # On the unfixed build nothing routes touch into the scroll machinery,
        # so the slider stays pinned and this assertion fails — the bug.
        after_touch = _scroll_state(page)
        assert after_touch["sliderTop"] < repinned["sliderTop"] - 1, (
            "touch swipe did not scroll the terminal scrollback: the view "
            f"stayed pinned to the live bottom (sliderTop "
            f"{repinned['sliderTop']} -> {after_touch['sliderTop']})"
        )
    finally:
        context.close()
