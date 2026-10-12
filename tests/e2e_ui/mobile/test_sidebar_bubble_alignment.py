"""E2E: the sidebar Search bubble and the chat-header overflow bubble align.

With the mobile sidebar drawer open, the drawer stops 56px short of the right
screen edge, so the chat header's round overflow (...) button stays visible in
the exposed strip directly beside the sidebar's round Search button. The two
bubbles read as one row of paired top-right controls, so they must share a
vertical centerline. Guarded on both surfaces where they diverge, in opposite
directions:

1. Thin web: the sidebar header row must match the mobile chat header height
   (h-14 below ``md``) or their 44px chips center at different heights and
   Search rides higher.
2. iOS shell: the drawer's safe-area top padding must mirror the chat
   header's ``safe-top - 0.5rem`` offset (index.css) or the offset flips and
   Search lands lower than the overflow bubble.

The iOS case runs in Chromium with the CDP safe-area override and the
``window.omnigentNative`` iOS-bridge stub (same stand-in as the sibling
``test_ios_*`` tests); the geometry under test is plain CSS layout, not
WebKit-specific rendering.
"""

from __future__ import annotations

import os

from playwright.sync_api import Browser, BrowserContext, Page, expect

# iPhone-class portrait viewport, below the Tailwind ``md`` breakpoint so the
# sidebar behaves as an overlay drawer and every ``max-md:`` rule is live.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}

# iPhone 15-class portrait insets: 59px status bar / Dynamic Island on top.
_IOS_SAFE_AREA = {"top": 59, "left": 0, "bottom": 34, "right": 0}

_IOS_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "ios",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onOpenPath: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
  onSidebarDrag: function () { return function () {}; },
  onViewModeChanged: function () { return function () {}; },
  setViewMode: function () {},
  setServerSwitcherHidden: function () {},
  setSidebarOpen: function () {},
};
"""

# Half a pixel absorbs subpixel layout rounding; the reported divergence is a
# full grid step (4px), so anything above 1px is a real misalignment.
_ALIGN_TOLERANCE_PX = 1.0


def _new_mobile_page(browser: Browser) -> tuple[BrowserContext, Page]:
    context = browser.new_context(
        viewport=_MOBILE_VIEWPORT,
        has_touch=True,
        is_mobile=True,
        record_video_dir=os.environ.get("OMNIGENT_E2E_RECORD_DIR"),
    )
    return context, context.new_page()


def _beat(page: Page) -> None:
    """Pause briefly between journey steps -- only while filming a clip."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(900)


def _open_drawer(page: Page, base_url: str, session_id: str) -> None:
    """Load the session at phone width and open the sidebar drawer."""
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    assert page.evaluate("matchMedia('(max-width: 767.98px)').matches"), (
        "expected the mobile (max-md) layout branch to be in effect"
    )

    toggle = page.get_by_role("button", name="Open sidebar")
    expect(toggle).to_be_visible(timeout=10_000)
    _beat(page)
    toggle.tap()
    drawer = page.locator('aside[aria-label="Conversations"]')
    expect(drawer).to_have_attribute("aria-hidden", "false", timeout=5_000)
    # Let the drawer's slide transition settle before measuring geometry.
    page.wait_for_timeout(700)
    _beat(page)


def _assert_bubbles_share_centerline(page: Page) -> None:
    search = page.get_by_test_id("sidebar-search-button")
    overflow = page.get_by_test_id("header-conversation-actions")
    expect(search).to_be_visible()
    expect(overflow).to_be_visible()

    search_box = search.bounding_box()
    overflow_box = overflow.bounding_box()
    assert search_box is not None and overflow_box is not None

    search_center = search_box["y"] + search_box["height"] / 2
    overflow_center = overflow_box["y"] + overflow_box["height"] / 2
    offset = search_center - overflow_center
    assert abs(offset) <= _ALIGN_TOLERANCE_PX, (
        f"Search bubble (center y={search_center:.1f}) sits "
        f"{abs(offset):.1f}px {'higher' if offset < 0 else 'lower'} than the "
        f"overflow bubble (center y={overflow_center:.1f}); the paired "
        f"top-right bubbles must share a vertical centerline"
    )


def test_bubbles_share_centerline_on_thin_web(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Thin web UI: Search and overflow bubbles align beside the open drawer."""
    base_url, session_id = seeded_session
    context, page = _new_mobile_page(browser)
    try:
        _open_drawer(page, base_url, session_id)
        _assert_bubbles_share_centerline(page)
        _beat(page)
    finally:
        context.close()


def test_bubbles_share_centerline_in_ios_shell(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """iOS shell with a status-bar inset: the same bubbles still align."""
    base_url, session_id = seeded_session
    context, page = _new_mobile_page(browser)
    try:
        page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
        cdp = context.new_cdp_session(page)
        cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": _IOS_SAFE_AREA})

        _open_drawer(page, base_url, session_id)
        expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")
        _assert_bubbles_share_centerline(page)
        _beat(page)
    finally:
        context.close()
