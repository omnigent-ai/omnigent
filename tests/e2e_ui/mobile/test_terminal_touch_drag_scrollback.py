"""Mobile: touch-dragging the terminal view must scroll back through scrollback.

On a phone, the terminal session view cannot be scrolled back with a finger:
touch-dragging over the xterm pane does nothing and the view stays pinned to
the live bottom, so output that has scrolled off screen is unreachable. A
mouse wheel over the same pane scrolls fine, so the scrollback exists
client-side (xterm is configured with a 20k-line buffer) — the gap is input
plumbing: ``TerminalSession.ts`` wires an explicitly mouse-only custom wheel
handler and nothing routes touch into xterm's scrollback, while
``TerminalView.tsx`` mounts xterm under ``overflow-hidden`` wrappers, which
also rules out the browser's native touch scrolling.

Journey (from the bug report, driven on the web lane at a touch phone
profile — the iOS/Android apps are thin native shells over this same SPA):

1. open a session whose agent declares shell access, on a touch phone viewport
2. header kebab → Shells → "New shell" — a real PTY opens as the main view
3. run a command that prints more output than fits on one screen
4. sanity: a mouse-wheel scroll moves the scrollback (it exists and works)
5. touch-drag down on the terminal → the scrollback must move too

The last assertion is the regression guard: on the buggy build a trusted
touch drag leaves the terminal pinned at the bottom while the same pane
scrolls freely under the wheel.

Scroll position is observed through the vertical scrollbar's slider inside
xterm's ``.xterm-scrollable-element`` (xterm v6 renders its own scrollbar;
the legacy ``.xterm-viewport`` no longer carries a native scroll range).
The slider's ``top`` falls as the view scrolls back and its height shrinks
as the buffer grows, so it mirrors both scrollback existence and position.

The drag is injected with CDP ``Input.dispatchTouchEvent`` (trusted,
compositor-level touch — what Chromium's device emulation produces), not
synthesized JS ``TouchEvent``s: untrusted events can never drive native
scrolling, and the guard must hold whichever way the fix lands (a JS touch
handler or permissive ``overflow``/``touch-action`` CSS).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from playwright.sync_api import Browser, Page, expect

# iPhone-13-class portrait profile with touch: matches the report's surface
# (reproduced in the iOS app, "expected to affect Android and mobile web
# browsers as well" — any touch browser over this same SPA).
_VIEWPORT = {"width": 390, "height": 844}

# Prints well past one phone screen of terminal rows, filling scrollback.
_FILL_COMMAND = "seq 1 200"

# The touch drag travels this many pixels down the pane. A working build
# scrolls the scrollback by roughly this distance (native touch scrolling)
# or a line-quantized equivalent (a scrollLines-based handler).
_DRAG_PX = 240

# Minimum slider movement (px) a scroll must produce to count. The 200-line
# buffer maps ~17 buffer px to ~1 slider px here, so 2 slider px is already
# several lines of scrollback — lenient to either fix shape, while the buggy
# build moves 0.
_MIN_SLIDER_PX = 2

# Scroll state of the visible terminal, read from xterm's own scrollbar.
# A hidden pre-warmed terminal surface can also be mounted, so pick the
# view with a real box. ``sliderTop``/``sliderHeight`` are null until the
# buffer overflows enough for xterm to render the vertical scrollbar.
_SCROLL_METRICS = """
() => {
  const views = Array.from(document.querySelectorAll('[data-testid="terminal-view"]'));
  const view = views.find((v) => v.getBoundingClientRect().height > 0);
  if (!view) return null;
  const screen = view.querySelector('.xterm-screen');
  if (!screen) return null;
  const rect = screen.getBoundingClientRect();
  const track = view.querySelector('.xterm-scrollable-element > .scrollbar.vertical');
  const slider = track ? track.querySelector('.slider') : null;
  const sliderRect = slider ? slider.getBoundingClientRect() : null;
  return {
    sliderTop: slider ? parseFloat(slider.style.top) || 0 : null,
    sliderHeight: sliderRect ? sliderRect.height : null,
    trackHeight: track ? track.getBoundingClientRect().height : null,
    centerX: rect.x + rect.width / 2,
    centerY: rect.y + rect.height / 2,
  };
}
"""


def _metrics(page: Page) -> dict[str, float] | None:
    """Scrollbar metrics + center point of the visible terminal, if any."""
    return page.evaluate(_SCROLL_METRICS)


def _wait_for_metrics(
    page: Page,
    predicate: Callable[[dict[str, float]], bool],
    what: str,
    timeout_s: float = 30.0,
) -> dict[str, float]:
    """Poll the visible terminal's scrollbar metrics until ``predicate`` holds.

    :param page: Page with the terminal view open.
    :param predicate: Condition on the metrics dict to wait for.
    :param what: Human description for the timeout error.
    :param timeout_s: Give-up horizon in seconds.
    :returns: The first metrics dict satisfying ``predicate``.
    """
    last: dict[str, float] | None = None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        last = _metrics(page)
        if last is not None and predicate(last):
            return last
        page.wait_for_timeout(200)
    raise AssertionError(f"timed out waiting for {what}; last scroll metrics: {last}")


def _has_scrollback(m: dict[str, float]) -> bool:
    """Whether the buffer overflows the screen by a few screens' worth."""
    return (
        m["sliderHeight"] is not None
        and m["sliderHeight"] > 0
        and m["trackHeight"] is not None
        and m["sliderHeight"] < m["trackHeight"] / 2
    )


def _pinned_to_bottom(m: dict[str, float]) -> bool:
    """Whether the view sits at the live bottom (slider at the track's end)."""
    return _has_scrollback(m) and m["sliderTop"] + m["sliderHeight"] >= m["trackHeight"] - 3


def test_touch_drag_scrolls_terminal_scrollback(
    browser: Browser,
    terminal_session: tuple[str, str],
) -> None:
    """A finger drag over the terminal scrolls back through its scrollback.

    Failure mode this catches: the terminal pane only wires mouse input, so
    on a touch device the scrollback is unreachable — the wheel sanity step
    passes while the trusted touch drag leaves the scrollbar slider pinned
    at the bottom, and this test fails on its final assertion.

    :param browser: Playwright browser to open the touch phone context on.
    :param terminal_session: ``(base_url, session_id)`` for a runner-bound
        session whose agent declares a shell terminal.
    """
    base_url, session_id = terminal_session

    ctx_kwargs: dict = {
        "viewport": _VIEWPORT,
        "has_touch": True,
        "is_mobile": True,
    }
    # Film the journey when the recording harness asks for it. The autouse
    # _record_video fixture only patches the async API, and this test drives
    # the sync API through its own context, so honor the env var directly.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        ctx_kwargs["record_video_dir"] = record_dir

    context = browser.new_context(**ctx_kwargs)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        # Step 2: header kebab → Shells → "New shell". On a phone the header
        # carries one kebab (owner menu, else the fallback session menu).
        kebab = page.get_by_test_id("header-conversation-actions").or_(
            page.get_by_test_id("session-actions-menu")
        )
        expect(kebab).to_be_visible(timeout=60_000)
        kebab.click()
        page.get_by_role("menuitem", name="Shells").click()
        drawer = page.get_by_test_id("shells-panel-drawer")
        expect(drawer).to_be_visible(timeout=10_000)
        drawer.get_by_role("button", name="New shell").click()

        # The created shell's xterm mounts and its WebSocket connects.
        connected = page.locator('[data-testid="terminal-view"][data-state="connected"]')
        expect(connected.first).to_be_visible(timeout=60_000)

        # Step 3: fill the scrollback well past one screen. Focus xterm's
        # hidden input (a container click doesn't reliably focus the WebGL
        # canvas in headless Chromium), type the command, and wait until the
        # buffer overflows — xterm renders its vertical scrollbar and the
        # slider shrinks below half the track.
        textarea = connected.first.locator("textarea.xterm-helper-textarea")
        textarea.focus()
        page.keyboard.type(_FILL_COMMAND, delay=40)
        page.keyboard.press("Enter")
        m = _wait_for_metrics(
            page,
            _has_scrollback,
            "the command output to overflow one screen into scrollback",
        )
        m = _wait_for_metrics(
            page,
            _pinned_to_bottom,
            "the live terminal to pin to the bottom after output",
        )
        pinned_top = m["sliderTop"]
        cx, cy = m["centerX"], m["centerY"]

        # Step 4 (sanity, holds before and after any fix): a mouse wheel over
        # the pane scrolls back — the slider leaves the bottom. Proves the
        # scrollback exists and is scrollable at all, so the final failure
        # is specifically touch.
        page.mouse.move(cx, cy)
        page.mouse.wheel(0, -600)
        _wait_for_metrics(
            page,
            lambda mm: mm["sliderTop"] is not None
            and mm["sliderTop"] < pinned_top - _MIN_SLIDER_PX,
            "the wheel scroll to move the scrollback (sanity precondition)",
            timeout_s=10.0,
        )

        # Return to the live bottom so the touch drag starts from the same
        # pinned state the wheel did.
        page.mouse.wheel(0, 60_000)
        m = _wait_for_metrics(
            page,
            _pinned_to_bottom,
            "the view to re-pin to the live bottom after the sanity scroll",
            timeout_s=10.0,
        )
        pinned_top = m["sliderTop"]

        # Step 5: one deliberate downward finger drag over the pane — the
        # universal "show me what scrolled off" gesture. Trusted touch via
        # CDP, stepped at ~60fps so it reads as a drag, not a tap.
        cdp = context.new_cdp_session(page)
        start_y = cy - _DRAG_PX / 2
        end_y = cy + _DRAG_PX / 2
        steps = 12
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [{"x": cx, "y": start_y}]},
        )
        for i in range(1, steps + 1):
            y = start_y + (end_y - start_y) * i / steps
            cdp.send(
                "Input.dispatchTouchEvent",
                {"type": "touchMove", "touchPoints": [{"x": cx, "y": y}]},
            )
            page.wait_for_timeout(16)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        # Let any momentum scrolling / batched handler work settle.
        page.wait_for_timeout(750)

        after = _metrics(page)
        assert after is not None, "the terminal view disappeared after the touch drag"
        scrolled_px = pinned_top - (after["sliderTop"] or pinned_top)
        assert scrolled_px >= _MIN_SLIDER_PX, (
            f"touch-dragging {_DRAG_PX}px down the terminal moved its scrollbar "
            f"slider by {scrolled_px:.1f}px — the view stayed pinned to the live "
            f"bottom (slider top {after['sliderTop']} of track "
            f"{after['trackHeight']}), while the same pane scrolled fine under "
            f"the mouse wheel; on a phone the terminal scrollback is unreachable"
        )
    finally:
        context.close()
