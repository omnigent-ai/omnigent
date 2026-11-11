"""Swiping left closes the open mobile sidebar drawer, as in the iOS app.

The SPA runs at a phone viewport with an iOS bridge stub; the native open swipe
is replayed through ``window.__omnigentNativeEmitSidebarDrag`` and the close
swipe uses real CDP touch events.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import CDPSession, Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract

_DRAWER = 'aside[aria-label="Conversations"]'
_LIST = f"{_DRAWER} nav.overflow-y-auto"

# Unlike the read-only iOS stubs elsewhere, this defines the page function the
# native shell evaluates for each frame of its left-edge swipe.
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


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 390, "height": 844},
        "has_touch": True,
        "is_mobile": True,
    }


def _open_app(page: Page, chat: ChatSessionContract) -> CDPSession:
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    page.goto(chat.url)
    expect(page.get_by_label("Message the agent")).to_be_visible()
    expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")
    return page.context.new_cdp_session(page)


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
    page.evaluate("() => window.__omnigentNativeEmitSidebarDrag('open', 0.68)")
    _expect_drawer_open(page)


def _drag(
    cdp: CDPSession,
    page: Page,
    *,
    x: float,
    y: float,
    dx: float = 0,
    dy: float = 0,
    steps: int,
    step_ms: int,
) -> float:
    """Drag one finger by (dx, dy); return the drawer's left edge just before lifting."""
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for step in range(1, steps + 1):
        point = {"x": x + dx * step / steps, "y": y + dy * step / steps}
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
        page.wait_for_timeout(step_ms)
    left_before_lift = _drawer_left(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    return left_before_lift


def test_swipe_left_closes_open_drawer(page: Page, chat_session_contract) -> None:
    """CUJ-1: a left swipe on a row, or on the exposed chat strip, closes the drawer."""
    chat = chat_session_contract
    chat.seed_sidebar_sessions([f"Swipe demo session {index:02d}" for index in range(1, 7)])
    cdp = _open_app(page, chat)

    _native_edge_swipe_open(page)
    row = page.locator(f"{_DRAWER} a", has_text="Swipe demo session 02")
    expect(row).to_be_visible()
    box = row.bounding_box()
    assert box is not None

    left_before_lift = _drag(
        cdp,
        page,
        x=box["x"] + box["width"] * 0.8,
        y=box["y"] + box["height"] / 2,
        dx=-150,
        steps=15,
        step_ms=30,
    )
    # Slow enough to close by distance (past a third of the drawer), not as a flick.
    assert -170 < left_before_lift < -120, (
        f"the drawer did not track the finger: {left_before_lift}"
    )
    _expect_drawer_closed(page)
    # The swipe must not also open the row it started on.
    assert page.url == chat.url

    page.get_by_role("button", name="Open sidebar").tap()
    _expect_drawer_open(page)
    strip = page.get_by_test_id("sidebar-scrim")
    expect(strip).to_be_visible()
    _drag(cdp, page, x=390 - 20, y=500, dx=-240, steps=16, step_ms=30)
    _expect_drawer_closed(page)
    expect(page.get_by_label("Message the agent")).to_be_visible()


def test_drawer_stays_open_for_scroll_and_short_drag(page: Page, chat_session_contract) -> None:
    """CUJ-2: scrolling the list or a short, slow drag leaves the drawer open."""
    chat = chat_session_contract
    chat.seed_sidebar_sessions([f"Scroll demo session {index:02d}" for index in range(1, 23)])
    cdp = _open_app(page, chat)

    page.get_by_role("button", name="Open sidebar").tap()
    _expect_drawer_open(page)
    expect(page.get_by_text("Scroll demo session 01")).to_be_visible()

    _drag(cdp, page, x=180, y=620, dy=-320, steps=14, step_ms=16)
    page.wait_for_function(f"() => document.querySelector('{_LIST}').scrollTop > 50")
    _expect_drawer_open(page)

    left_before_lift = _drag(cdp, page, x=260, y=420, dx=-70, steps=10, step_ms=60)
    assert left_before_lift < -30, f"the drawer did not follow the short drag: {left_before_lift}"
    _expect_drawer_open(page)
    assert page.url == chat.url
