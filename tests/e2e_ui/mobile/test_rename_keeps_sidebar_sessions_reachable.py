"""iOS sidebar must stay scrollable/reachable while renaming a session.

Reported journey: open the app on an iPhone -> open the left sidebar drawer
(session list overflows the screen) -> long-press a session row -> Rename (the
inline edit field focuses, raising the soft keyboard) -> touch-scroll the list
-> the sidebar no longer scrolls, preventing access to other sessions.

The iOS app is a thin native shell (WKWebView) over this same server-served
SPA, so the journey is driven on the web lane at a phone viewport with the
suite's standard ``window.omnigentNative = {kind: "ios"}`` bridge stub (the
feature-detection path ``test_ios_switcher_in_header.py`` uses). The one
environmental input a browser cannot produce by itself -- the soft keyboard --
is simulated exactly the way WebKit publishes it to the page: the
``window.visualViewport`` height shrinks and fires ``resize``, which is the
signal the app's own iOS keyboard handling (``useIOSViewportLock``,
``useIOSNativeKeyboardInset``) consumes.

Contract under test (the user-observable claim): while the rename edit is
active with the keyboard up, the sidebar session list must still scroll under
touch AND must be able to bring every session row into the visible area above
the keyboard. The regression this guards: the drawer is a ``fixed inset-0``
overlay sized to the full layout viewport, which the iOS shell-lock
intentionally does not resize (see ``useIOSNativeKeyboardInset``'s own doc
comment -- fixed full-viewport overlays like the mobile TerminalsPanel pad
themselves), so without the drawer consuming the keyboard inset the bottom of
the list continues underneath the keyboard and its last rows can never be
scrolled into view while renaming.
"""

from __future__ import annotations

import json
import os

import httpx
from playwright.sync_api import Browser, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle

# iPhone-13-class portrait profile with touch.
_VIEWPORT = {"width": 390, "height": 844}

# Representative iPhone portrait soft-keyboard height in CSS px.
_KEYBOARD_HEIGHT = 336

# Enough sessions that the drawer's list overflows a phone screen by several
# hundred px, so "access to other sessions" genuinely depends on scrolling.
_FILLER_COUNT = 22

_LIST_SELECTOR = 'aside[aria-label="Conversations"] nav.overflow-y-auto'

# Minimal stand-in for the iOS WKWebView bridge (``web/ios``'s injected
# ``window.omnigentNative``), mirroring test_ios_switcher_in_header.py. Runs
# before any app script so ``isIOSShell()`` sees the iOS shell and the SPA
# applies its iOS-native chrome + keyboard handling.
_IOS_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "ios",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onOpenPath: function () { return function () {}; },
  onSidebarDrag: function () { return function () {}; },
  setServerSwitcherHidden: function () {},
  setViewMode: function () {},
  onViewModeChanged: function () { return function () {}; },
  onNativeInsets: function (callback) {
    callback({ topBar: 36, bottomBar: 48 });
    return function () {};
  },
};
"""

# Controllable stand-in for the iOS soft keyboard: wraps the real
# visualViewport in a fake whose height can be shrunk on demand, firing the
# same ``resize`` events WebKit fires when the keyboard opens. The app reads
# ``window.visualViewport`` (useIOSViewportLock / useIOSNativeKeyboardInset),
# so shrinking the fake exercises exactly the code path the real keyboard
# drives -- the app's reaction (shell resize, insets, or the lack of them) is
# entirely its own.
_FAKE_VISUAL_VIEWPORT = """
(() => {
  const real = window.visualViewport;
  if (!real) return;
  const listeners = { resize: new Set(), scroll: new Set() };
  let heightOverride = null;
  const fake = {
    get width() { return real.width; },
    get height() { return heightOverride ?? real.height; },
    get offsetLeft() { return real.offsetLeft; },
    get offsetTop() { return real.offsetTop; },
    get pageLeft() { return real.pageLeft; },
    get pageTop() { return real.pageTop; },
    get scale() { return real.scale; },
    addEventListener(type, cb) { (listeners[type] ??= new Set()).add(cb); },
    removeEventListener(type, cb) { listeners[type]?.delete(cb); },
    dispatchEvent() { return true; },
    __fire(type) { for (const cb of [...(listeners[type] ?? [])]) cb({ type }); },
  };
  real.addEventListener('resize', () => fake.__fire('resize'));
  real.addEventListener('scroll', () => fake.__fire('scroll'));
  Object.defineProperty(window, 'visualViewport', { get: () => fake, configurable: true });
  window.__setKeyboardHeight = (kb) => {
    heightOverride = kb ? real.height - kb : null;
    fake.__fire('resize');
  };
})();
"""


def _seed_filler_sessions(base_url: str, count: int) -> list[str]:
    """Create ``count`` titled sessions so the sidebar list overflows."""
    ids: list[str] = []
    bundle = _build_hello_world_bundle()
    for i in range(count):
        resp = httpx.post(
            f"{base_url}/v1/sessions",
            data={"metadata": json.dumps({})},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
            timeout=30.0,
        )
        resp.raise_for_status()
        sid = resp.json()["session_id"]
        httpx.patch(
            f"{base_url}/v1/sessions/{sid}",
            json={"title": f"Filler session {i:02d}"},
            timeout=10.0,
        ).raise_for_status()
        ids.append(sid)
    return ids


def _list_scroll_top(page) -> float:
    return page.evaluate(f"() => document.querySelector('{_LIST_SELECTOR}').scrollTop")


def _list_metrics(page) -> dict:
    return page.evaluate(
        f"""
        () => {{
          const el = document.querySelector('{_LIST_SELECTOR}');
          const r = el.getBoundingClientRect();
          return {{
            scrollTop: el.scrollTop,
            scrollHeight: el.scrollHeight,
            clientHeight: el.clientHeight,
            x: r.x, y: r.y, w: r.width, h: r.height,
          }};
        }}
        """
    )


def _touch_scroll(cdp, page, x: float, y: float, dy: float, steps: int = 8) -> None:
    """Drag one finger from (x, y) by ``dy`` CSS px via real CDP touch events."""
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
    )
    for i in range(1, steps + 1):
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [{"x": x, "y": y + dy * i / steps}]},
        )
        page.wait_for_timeout(16)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(120)


def _long_press(cdp, page, x: float, y: float, hold_ms: int = 850) -> None:
    """Press-and-hold one finger -- the touch gesture that opens a row's menu."""
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
    )
    page.wait_for_timeout(hold_ms)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def test_rename_keeps_sidebar_sessions_reachable(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """While a rename edit is active, every sidebar session must stay reachable.

    Failure mode this guards: the sidebar drawer is a fixed
    full-layout-viewport overlay that ignores the iOS soft keyboard -- the
    shell shrinks to the visual viewport but the drawer and its scroll pane do
    not, so with the rename field focused (keyboard up) the last several
    session rows sit permanently behind the keyboard and no amount of
    scrolling can reveal them: "preventing access to other sessions".

    :param browser: Playwright browser to open a touch phone context on.
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    _seed_filler_sessions(base_url, _FILLER_COUNT)

    ctx_kwargs: dict = {"viewport": _VIEWPORT, "has_touch": True, "is_mobile": True}
    # Film the journey when the recording harness asks for it. The autouse
    # _record_video fixture only patches the async API, and this test drives
    # the sync API through its own context, so honor the env var directly.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        ctx_kwargs["record_video_dir"] = record_dir

    context = browser.new_context(**ctx_kwargs)
    try:
        page = context.new_page()
        page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
        page.add_init_script(_FAKE_VISUAL_VIEWPORT)
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.locator('textarea[aria-label="Message the agent"]')).to_be_visible(
            timeout=60_000
        )
        expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")

        # Open the sidebar drawer and wait for its slide-in to settle.
        page.locator('button[aria-label="Open sidebar"]').click()
        expect(page.get_by_text("Filler session 00", exact=False)).to_be_visible(timeout=10_000)
        page.wait_for_function(
            f"() => document.querySelector('{_LIST_SELECTOR}').getBoundingClientRect().x > -1"
        )
        page.wait_for_timeout(300)
        page.evaluate(f"() => document.querySelector('{_LIST_SELECTOR}').scrollTo(0, 0)")
        page.wait_for_timeout(200)

        metrics = _list_metrics(page)
        print(f"[rename-scroll] list metrics after open: {metrics}")
        assert metrics["scrollHeight"] > metrics["clientHeight"] + 100, (
            f"precondition failed: list does not overflow the screen: {metrics}"
        )

        cdp = context.new_cdp_session(page)
        cx = metrics["x"] + metrics["w"] / 2

        # Control: before any rename, the list scrolls under a touch drag.
        _touch_scroll(cdp, page, cx, 430, -250)
        page.wait_for_timeout(300)
        control_scroll = _list_scroll_top(page)
        print(f"[rename-scroll] control scrollTop after one touch drag: {control_scroll}")
        assert control_scroll > 50, (
            f"control failed: the list did not scroll before renaming (scrollTop={control_scroll})"
        )
        page.evaluate(f"() => document.querySelector('{_LIST_SELECTOR}').scrollTo(0, 0)")
        page.wait_for_timeout(200)

        # Long-press an in-viewport filler row: the touch path to row actions.
        box = None
        row_label = None
        for handle in page.locator('aside[aria-label="Conversations"] a[href^="/c/"]').all():
            b = handle.bounding_box()
            text = (handle.inner_text() or "").strip()
            if b and 200 < b["y"] < 450 and "Filler" in text:
                box = b
                row_label = text
                break
        assert box is not None, "no in-viewport filler row found to long-press"
        print(f"[rename-scroll] long-pressing row {row_label!r} at {box}")
        _long_press(cdp, page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

        menu = page.locator('[role="menu"][data-state="open"]')
        expect(menu).to_be_visible(timeout=5_000)

        # Tap Rename: the inline edit field replaces the row and focuses.
        rename_item = page.get_by_test_id("rename-conversation")
        expect(rename_item).to_be_visible()
        rename_item.tap()
        edit = page.get_by_test_id("rename-conversation-input")
        expect(edit).to_be_visible(timeout=5_000)
        expect(edit).to_be_focused()

        # The focused field raises the soft keyboard: WebKit shrinks the
        # visual viewport and fires resize; the app reacts with its own iOS
        # keyboard handling (the shell locks to the visual viewport).
        page.evaluate(f"() => window.__setKeyboardHeight({_KEYBOARD_HEIGHT})")
        page.wait_for_timeout(400)
        keyboard_top = page.evaluate("() => window.visualViewport.height")
        shell_h = page.evaluate(
            "() => document.querySelector('.app-shell').getBoundingClientRect().height"
        )
        print(f"[rename-scroll] keyboard open: visible height {keyboard_top}, shell {shell_h}")

        # The reported failure: touch-scroll the list while the rename edit is
        # active. Gestures stay inside the keyboard-visible upper region, where
        # the user's finger actually is.
        _touch_scroll(cdp, page, cx, 430, -250)
        page.wait_for_timeout(300)
        first_attempt = _list_scroll_top(page)
        print(f"[rename-scroll] scrollTop after first drag while renaming: {first_attempt}")

        # Keep flicking until the list stops moving (bounded), i.e. the user
        # scrolls as far down as the drawer will ever let them.
        prev = -1.0
        for _ in range(12):
            top = _list_scroll_top(page)
            if top == prev:
                break
            prev = top
            _touch_scroll(cdp, page, cx, 430, -250)
            page.wait_for_timeout(250)
        final = _list_metrics(page)
        last_row_bottom = page.evaluate(
            """
            () => {
              const links = [...document.querySelectorAll(
                'aside[aria-label="Conversations"] a[href^="/c/"]')];
              let maxBottom = 0;
              for (const a of links) {
                const r = a.getBoundingClientRect();
                if (r.height > 0) maxBottom = Math.max(maxBottom, r.bottom);
              }
              return maxBottom;
            }
            """
        )
        print(f"[rename-scroll] fully scrolled while renaming: {final}")
        print(f"[rename-scroll] last row bottom {last_row_bottom} vs keyboard top {keyboard_top}")

        # The journey is still "while renaming": the edit must not have been
        # committed/cancelled by the scroll attempts themselves.
        expect(edit).to_be_visible()

        # Half 1 -- the list must respond to touch at all while renaming.
        assert first_attempt > 50, (
            "the sidebar list stopped responding to touch scrolling "
            f"while the rename edit is active (scrollTop={first_attempt} after a "
            "250px drag)"
        )

        # Half 2 -- scrolling must be able to reach every session. With the
        # keyboard up, the last row must fit above the keyboard once the list
        # is scrolled to its limit; otherwise the sessions at the bottom are
        # unreachable for as long as the rename field is active.
        assert final["scrollTop"] + final["clientHeight"] >= final["scrollHeight"] - 2, (
            f"list never reached its scroll limit: {final}"
        )
        assert last_row_bottom <= keyboard_top + 2, (
            "while the session rename field is active (soft keyboard "
            "up), the sidebar drawer ignores the keyboard inset: the shell "
            f"shrinks to {keyboard_top}px but the drawer's session list still "
            f"extends to {last_row_bottom}px, so the bottom rows sit behind the "
            "keyboard and cannot be scrolled into view -- other sessions are "
            "inaccessible while renaming."
        )
    finally:
        context.close()
