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

A second journey renames a row in the lower half of the screen. Once the
keyboard rises, the focused field and its Save and Cancel controls must sit in
the list's visible area above the keyboard. The iOS shell disables document
scrolling while the app owns keyboard layout, so WebKit's native focus reveal
does not move the inner list; the app must scroll it.
"""

from __future__ import annotations

import os

import httpx
from playwright.sync_api import Browser, BrowserContext, CDPSession, Locator, Page, expect

from tests._helpers.session import post_session_bundle
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


def _seed_filler_sessions(
    base_url: str, count: int, title_prefix: str = "Filler session"
) -> list[str]:
    """Create ``count`` titled sessions so the sidebar list overflows."""
    ids: list[str] = []
    bundle = _build_hello_world_bundle()
    for i in range(count):
        resp = post_session_bundle(httpx.post, f"{base_url}/v1/sessions", bundle, timeout=30.0)
        resp.raise_for_status()
        sid = resp.json()["session_id"]
        httpx.patch(
            f"{base_url}/v1/sessions/{sid}",
            json={"title": f"{title_prefix} {i:02d}"},
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


def _new_phone_context(browser: Browser) -> BrowserContext:
    """Open a touch phone context, filmed when the recording harness asks.

    The autouse ``_record_video`` fixture only patches the async API, and these
    journeys drive the sync API through their own context, so honor the env var
    directly.
    """
    ctx_kwargs: dict = {"viewport": _VIEWPORT, "has_touch": True, "is_mobile": True}
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        ctx_kwargs["record_video_dir"] = record_dir
    return browser.new_context(**ctx_kwargs)


def _open_ios_drawer(
    context: BrowserContext, base_url: str, session_id: str, row_text: str
) -> Page:
    """Load a session as the iOS shell and open the drawer, scrolled to the top.

    :param row_text: Text of a seeded row that shows once the drawer is open.
    """
    page = context.new_page()
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    page.add_init_script(_FAKE_VISUAL_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator('textarea[aria-label="Message the agent"]')).to_be_visible(timeout=60_000)
    expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")

    # Open the sidebar drawer and wait for its slide-in to settle.
    page.locator('button[aria-label="Open sidebar"]').click()
    expect(page.get_by_text(row_text, exact=False)).to_be_visible(timeout=10_000)
    page.wait_for_function(
        f"() => document.querySelector('{_LIST_SELECTOR}').getBoundingClientRect().x > -1"
    )
    page.wait_for_timeout(300)
    page.evaluate(f"() => document.querySelector('{_LIST_SELECTOR}').scrollTo(0, 0)")
    page.wait_for_timeout(200)
    return page


def _find_row(
    page: Page, title_prefix: str, min_top: float, max_top: float
) -> tuple[dict, str] | None:
    """Return the first titled row whose top edge lies strictly between the bounds."""
    for handle in page.locator('aside[aria-label="Conversations"] a[href^="/c/"]').all():
        box = handle.bounding_box()
        text = (handle.inner_text() or "").strip()
        if box and min_top < box["y"] < max_top and title_prefix in text:
            return box, text
    return None


def _start_rename(cdp: CDPSession, page: Page, box: dict) -> Locator:
    """Long-press a row and choose Rename; return the focused inline edit field."""
    _long_press(cdp, page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    expect(page.locator('[role="menu"][data-state="open"]')).to_be_visible(timeout=5_000)

    # Tap Rename: the inline edit field replaces the row and focuses.
    rename_item = page.get_by_test_id("rename-conversation")
    expect(rename_item).to_be_visible()
    rename_item.tap()
    edit = page.get_by_test_id("rename-conversation-input")
    expect(edit).to_be_visible(timeout=5_000)
    expect(edit).to_be_focused()
    return edit


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

    context = _new_phone_context(browser)
    try:
        page = _open_ios_drawer(context, base_url, session_id, "Filler session 00")

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
        row = _find_row(page, "Filler session", 200, 450)
        assert row is not None, "no in-viewport filler row found to long-press"
        box, row_label = row
        print(f"[rename-scroll] long-pressing row {row_label!r} at {box}")
        edit = _start_rename(cdp, page, box)

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


# Distinct from the reachability journey's titles so both can share one server.
_KEYBOARD_ROW_PREFIX = "Keyboard rename row"


def _edit_geometry(page: Page) -> dict:
    return page.evaluate(
        f"""
        () => {{
          const input = document.querySelector('[data-testid="rename-conversation-input"]');
          const list = document.querySelector('{_LIST_SELECTOR}');
          const ir = input.getBoundingClientRect();
          const lr = list.getBoundingClientRect();
          // A control is usable when a tap at its center reaches it.
          const uncovered = (label) => {{
            const button = document.querySelector('button[aria-label="' + label + '"]');
            const r = button.getBoundingClientRect();
            const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            return !!hit && button.contains(hit);
          }};
          return {{
            inputTop: ir.top, inputBottom: ir.bottom,
            listTop: lr.top, listBottom: lr.bottom,
            listScrollTop: list.scrollTop,
            keyboardTop: window.visualViewport.height,
            focused: document.activeElement === input,
            saveUncovered: uncovered("Save rename"),
            cancelUncovered: uncovered("Cancel rename"),
          }};
        }}
        """
    )


def test_rename_scrolls_edit_row_into_view_above_keyboard(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """The focused rename field must be visible once the keyboard is up.

    :param browser: Playwright browser to open a touch phone context on.
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    _seed_filler_sessions(base_url, _FILLER_COUNT, title_prefix=_KEYBOARD_ROW_PREFIX)

    context = _new_phone_context(browser)
    try:
        page = _open_ios_drawer(context, base_url, session_id, f"{_KEYBOARD_ROW_PREFIX} 00")

        metrics = _list_metrics(page)
        keyboard_top = _VIEWPORT["height"] - _KEYBOARD_HEIGHT
        print(f"[rename-into-view] list metrics after open: {metrics}")

        # A fully visible row that the keyboard will cover once it rises.
        row = _find_row(
            page, _KEYBOARD_ROW_PREFIX, keyboard_top + 40, metrics["y"] + metrics["h"] - 40
        )
        assert row is not None, "no visible row below the future keyboard top"
        box, row_label = row
        print(f"[rename-into-view] long-pressing row {row_label!r} at {box}")

        cdp = context.new_cdp_session(page)
        _start_rename(cdp, page, box)
        before = _edit_geometry(page)
        print(f"[rename-into-view] edit focused, keyboard down: {before}")

        # The focused field raises the soft keyboard.
        page.evaluate(f"() => window.__setKeyboardHeight({_KEYBOARD_HEIGHT})")
        page.wait_for_timeout(400)
        after = _edit_geometry(page)
        print(f"[rename-into-view] keyboard up: {after}")

        assert after["focused"], f"rename field lost focus: {after}"
        visible_bottom = min(after["listBottom"], after["keyboardTop"])
        assert after["inputTop"] >= after["listTop"] - 1, (
            f"rename field scrolled above the list's visible area: {after}"
        )
        assert after["inputBottom"] <= visible_bottom + 1, (
            "while renaming with the soft keyboard up, the focused rename field "
            f"sits at y={after['inputTop']:.0f}-{after['inputBottom']:.0f}, below "
            f"the list's visible bottom ({visible_bottom:.0f}px). The list did not "
            f"scroll it into view (scrollTop {before['listScrollTop']} -> "
            f"{after['listScrollTop']})."
        )
        assert after["saveUncovered"] and after["cancelUncovered"], (
            f"the rename field's Save or Cancel control is covered by other drawer chrome: {after}"
        )
    finally:
        context.close()
