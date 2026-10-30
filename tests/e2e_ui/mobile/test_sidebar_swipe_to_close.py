"""Swiping left closes the open sidebar drawer in the iOS app.

The SPA runs at a phone viewport with an iOS bridge stub; the native open swipe
is replayed through ``window.__omnigentNativeEmitSidebarDrag`` and the close
swipe uses real CDP touch events.
"""

from __future__ import annotations

import os

from playwright.sync_api import Browser, CDPSession, Page, expect

from tests.e2e_ui.mobile.test_rename_keeps_sidebar_sessions_reachable import (
    _LIST_SELECTOR,
    _new_phone_context,
    _seed_filler_sessions,
    _touch_scroll,
)

_DRAWER = 'aside[aria-label="Conversations"]'

# iOS bridge stub. Unlike the read-only stubs elsewhere, it defines the page
# function the native shell evaluates for each frame of its left-edge swipe.
_IOS_SHELL_INIT_SCRIPT = """
(() => {
  const dragCallbacks = new Set();
  window.__omnigentNativeEmitSidebarDrag = (phase, progress) => {
    for (const callback of dragCallbacks) callback(phase, progress);
  };
  window.omnigentNative = {
    kind: "ios",
    setBadgeCount() {},
    notify() { return Promise.resolve(false); },
    onNotificationActivated() { return () => {}; },
    onOpenPath() { return () => {}; },
    onSidebarDrag(callback) {
      dragCallbacks.add(callback);
      return () => dragCallbacks.delete(callback);
    },
    setServerSwitcherHidden() {},
    setSidebarOpen() {},
    setViewMode() {},
    onViewModeChanged() { return () => {}; },
    onNativeInsets(callback) {
      callback({ topBar: 36, bottomBar: 48 });
      return () => {};
    },
  };
})();
"""

# Recording aid only: CDP touches leave no on-screen trace, so draw a dot under
# the finger. Passive and click-through, so it cannot affect the gesture.
_TOUCH_MARKER_SCRIPT = """
window.addEventListener("DOMContentLoaded", () => {
  const dot = document.createElement("div");
  dot.style.cssText = "position:fixed;z-index:2147483647;width:36px;height:36px;" +
    "margin:-18px 0 0 -18px;border-radius:50%;background:rgba(255,64,64,.45);" +
    "border:2px solid rgba(255,255,255,.9);pointer-events:none;display:none";
  document.body.appendChild(dot);
  const show = (event) => {
    const t = event.touches[0];
    if (!t) return;
    dot.style.left = t.clientX + "px";
    dot.style.top = t.clientY + "px";
    dot.style.display = "block";
  };
  window.addEventListener("touchstart", show, { passive: true, capture: true });
  window.addEventListener("touchmove", show, { passive: true, capture: true });
  window.addEventListener("touchend", () => { dot.style.display = "none"; }, { capture: true });
});
"""


_RECORDING = bool(os.environ.get("OMNIGENT_E2E_RECORD_DIR"))


def _demo_pause(page: Page, ms: int) -> None:
    """Hold the frame so a recorded demo is readable; not a synchronization point."""
    if _RECORDING:
        page.wait_for_timeout(ms)


def _open_app(page: Page, base_url: str, session_id: str) -> None:
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    if _RECORDING:
        page.add_init_script(_TOUCH_MARKER_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator('textarea[aria-label="Message the agent"]')).to_be_visible(timeout=60_000)
    expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")


def _drawer_left(page: Page) -> float:
    return page.evaluate(f"() => document.querySelector('{_DRAWER}').getBoundingClientRect().x")


def _expect_drawer_open(page: Page) -> None:
    expect(page.locator(_DRAWER)).not_to_have_attribute("data-collapsed", "true")
    page.wait_for_function(
        f"() => document.querySelector('{_DRAWER}').getBoundingClientRect().x > -1"
    )


def _expect_drawer_closed(page: Page) -> None:
    expect(page.locator(_DRAWER)).to_have_attribute("data-collapsed", "true")
    page.wait_for_function(
        f"() => document.querySelector('{_DRAWER}').getBoundingClientRect().right < 1"
    )


def _native_edge_swipe_open(page: Page) -> None:
    """Replay the iOS shell's left-edge swipe frames, then its open decision."""
    page.evaluate("() => window.__omnigentNativeEmitSidebarDrag('begin', 0.02)")
    for step in range(1, 16):
        page.evaluate(f"() => window.__omnigentNativeEmitSidebarDrag('move', {step * 0.045})")
        _demo_pause(page, 30)
    page.evaluate("() => window.__omnigentNativeEmitSidebarDrag('open', 0.68)")
    _expect_drawer_open(page)


def _swipe_left(
    cdp: CDPSession,
    page: Page,
    *,
    x: float,
    y: float,
    distance: float,
    steps: int,
    step_ms: int,
    pause_at_step: int | None = None,
) -> float | None:
    """Drag one finger left; return the drawer's left edge at the mid-drag pause."""
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    mid_drag_left = None
    for step in range(1, steps + 1):
        point = {"x": x - distance * step / steps, "y": y}
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
        page.wait_for_timeout(step_ms)
        if step == pause_at_step:
            mid_drag_left = _drawer_left(page)
            _demo_pause(page, 700)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    return mid_drag_left


def test_swipe_left_closes_open_drawer(browser: Browser, seeded_session: tuple[str, str]) -> None:
    """CUJ-1: a left swipe on the drawer, or on the exposed chat strip, closes it.

    :param browser: Playwright browser to open a touch phone context on.
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound session.
    """
    base_url, session_id = seeded_session
    _seed_filler_sessions(base_url, 6, title_prefix="Swipe demo session")

    context = _new_phone_context(browser)
    try:
        page = context.new_page()
        _open_app(page, base_url, session_id)
        cdp = context.new_cdp_session(page)
        _demo_pause(page, 800)

        _native_edge_swipe_open(page)
        row = page.locator(f"{_DRAWER} a", has_text="Swipe demo session 02")
        expect(row).to_be_visible()
        _demo_pause(page, 800)

        # Start on a session row and drag left: the drawer follows the finger.
        box = row.bounding_box()
        assert box is not None
        mid_drag_left = _swipe_left(
            cdp,
            page,
            x=box["x"] + box["width"] * 0.8,
            y=box["y"] + box["height"] / 2,
            distance=220,
            steps=22,
            step_ms=30,
            pause_at_step=10,
        )
        assert mid_drag_left is not None and -150 < mid_drag_left < -60, (
            f"the drawer did not track the finger mid-swipe (left edge {mid_drag_left})"
        )
        _expect_drawer_closed(page)
        # The swipe must not also open the row it started on.
        assert page.url.endswith(f"/c/{session_id}"), page.url
        _demo_pause(page, 1000)

        # Reopen from the header and swipe left from the exposed chat strip.
        page.get_by_role("button", name="Open sidebar").tap()
        _expect_drawer_open(page)
        _demo_pause(page, 800)
        strip = page.get_by_test_id("sidebar-scrim")
        expect(strip).to_be_visible()
        viewport = page.viewport_size
        assert viewport is not None
        viewport_width = viewport["width"]
        _swipe_left(cdp, page, x=viewport_width - 20, y=500, distance=240, steps=16, step_ms=30)
        _expect_drawer_closed(page)
        expect(page.locator('textarea[aria-label="Message the agent"]')).to_be_visible()
        _demo_pause(page, 1200)
    finally:
        context.close()


def test_drawer_stays_open_for_scroll_and_short_drag(
    browser: Browser, seeded_session: tuple[str, str]
) -> None:
    """CUJ-2: scrolling the list or a short, slow drag leaves the drawer open.

    :param browser: Playwright browser to open a touch phone context on.
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound session.
    """
    base_url, session_id = seeded_session
    _seed_filler_sessions(base_url, 22, title_prefix="Scroll demo session")

    context = _new_phone_context(browser)
    try:
        page = context.new_page()
        _open_app(page, base_url, session_id)
        cdp = context.new_cdp_session(page)
        _demo_pause(page, 800)

        page.get_by_role("button", name="Open sidebar").tap()
        _expect_drawer_open(page)
        expect(page.get_by_text("Scroll demo session", exact=False).first).to_be_visible()
        page.evaluate(f"() => document.querySelector('{_LIST_SELECTOR}').scrollTo(0, 0)")
        _demo_pause(page, 800)

        # A vertical swipe scrolls the session list and leaves the drawer open.
        _touch_scroll(cdp, page, 180, 620, -320, steps=14)
        page.wait_for_function(f"() => document.querySelector('{_LIST_SELECTOR}').scrollTop > 50")
        _expect_drawer_open(page)
        _demo_pause(page, 600)

        # A short, slow drag left moves the drawer, then springs it back open.
        mid_drag_left = _swipe_left(
            cdp, page, x=260, y=420, distance=70, steps=10, step_ms=60, pause_at_step=10
        )
        assert mid_drag_left is not None and mid_drag_left < -30, (
            f"the drawer did not follow the short drag (left edge {mid_drag_left})"
        )
        _expect_drawer_open(page)
        assert page.url.endswith(f"/c/{session_id}"), page.url
        _demo_pause(page, 1200)
    finally:
        context.close()
