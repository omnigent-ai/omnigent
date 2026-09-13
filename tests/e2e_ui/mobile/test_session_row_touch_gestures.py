"""Browser e2e: session-row touch gestures at a phone viewport.

The two phone-surface facets of the touch-input problem (the Android/iOS apps
are thin shells over this same server-served SPA, so the journeys are driven
on the web lane at a phone viewport):

- **Session rows have no swipe affordances.** Swiping across a row does
  nothing at all -- the row doesn't track the finger, no action affordance
  appears, and no archive/delete fires; the only path to those actions is a
  menu.
- **Long-press on session rows is flaky.** The row link, native scroll/drag,
  and the modal context menu compete for the gesture: the menu's long-press
  timer is cancelled by *any* pointer movement, so a realistic finger hold
  (with a couple px of wobble) never opens the menu and the trailing
  synthesized click navigates instead.

Both journeys drive trusted touch input through CDP
``Input.dispatchTouchEvent`` so the browser produces the same
pointer/touch/synthesized-click stream a real touchscreen does.
"""

from __future__ import annotations

import os
import re
import time

import httpx
from playwright.sync_api import Browser, BrowserContext, CDPSession, Page, expect

# iPhone-13-class portrait profile with touch.
_VIEWPORT = {"width": 390, "height": 844}

# Swipe travel across the row: most of the row's width, far past any slop.
_SWIPE_PX = 144
# "The row tracked the finger" means it moved meaningfully, not a rounding
# jitter.
_MIN_TRACK_PX = 24

# Long-press hold, comfortably past both the browser's and any component's
# long-press threshold (Radix uses 700ms).
_HOLD_SECONDS = 0.9
# Realistic finger wobble while holding: a couple of CSS px side to side.
_WOBBLE_PX = 2


def _touch(cdp: CDPSession, type_: str, points: list[dict[str, float]]) -> None:
    """Dispatch one trusted touch event via CDP.

    :param cdp: CDP session for the page under test.
    :param type_: ``touchStart`` / ``touchMove`` / ``touchEnd``.
    :param points: Active touch points (empty for ``touchEnd``).
    """
    cdp.send("Input.dispatchTouchEvent", {"type": type_, "touchPoints": points})


def _phone_context(browser: Browser) -> BrowserContext:
    """Open a touch phone context, honoring the recording harness."""
    kwargs: dict = {"viewport": _VIEWPORT, "has_touch": True, "is_mobile": True}
    # The autouse _record_video fixture only patches the async API; these
    # tests drive the sync API through their own context, so honor the env
    # var directly.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        kwargs["record_video_dir"] = record_dir
    return browser.new_context(**kwargs)


def _open_sidebar_drawer(page: Page) -> None:
    """Tap the chat header's hamburger and wait for the drawer to settle."""
    page.get_by_role("button", name="Open sidebar").tap()
    expect(page.locator('aside[aria-label="Conversations"]')).to_be_visible()
    # Let the drawer's slide-in animation finish so row geometry is stable.
    page.wait_for_timeout(400)


def _title_session(base_url: str, session_id: str, title: str) -> None:
    """Give the session a readable title so its row is identifiable."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_session_row_swipe_has_affordance(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Swiping left across a session row must do *something* user-visible.

    Journey: open the app at a phone viewport -> open the sidebar drawer ->
    swipe a finger left across a session row -> the row tracks the finger
    and/or a swipe action (archive/delete affordance) appears or commits.

    Failure mode this catches: rows have no swipe handling
    at all -- the row doesn't move, no affordance appears, no session write
    fires; the gesture is simply dead.

    :param browser: Playwright browser to open the touch phone context on.
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    _title_session(base_url, session_id, "Weekly report draft")

    context = _phone_context(browser)
    try:
        page = context.new_page()

        # Record any archive/delete the swipe might commit.
        session_writes: list[str] = []

        def _on_request(request) -> None:
            if request.method in ("PATCH", "DELETE") and f"/v1/sessions/{session_id}" in request.url:
                session_writes.append(f"{request.method} {request.post_data or ''}".strip())

        page.on("request", _on_request)
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.get_by_role("button", name="Open sidebar")).to_be_visible(timeout=30_000)
        _open_sidebar_drawer(page)

        row_link = page.locator(f'a[href="/c/{session_id}"]')
        expect(row_link).to_be_visible(timeout=30_000)
        row_li = page.locator("li").filter(has=row_link)
        aside = page.locator('aside[aria-label="Conversations"]')

        def _rel_x() -> float:
            """Row x relative to the drawer, so drawer motion can't alias."""
            link_box = row_link.bounding_box()
            aside_box = aside.bounding_box()
            assert link_box is not None and aside_box is not None
            return link_box["x"] - aside_box["x"]

        start_rel_x = _rel_x()
        box = row_link.bounding_box()
        assert box is not None
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2

        # Finger down mid-row, swipe left in 12px steps, sampling how far the
        # row tracks the finger, then lift.
        cdp = context.new_cdp_session(page)
        _touch(cdp, "touchStart", [{"x": x, "y": y, "id": 1}])
        max_track = 0.0
        step = 12
        for dx in range(step, _SWIPE_PX + 1, step):
            _touch(cdp, "touchMove", [{"x": x - dx, "y": y, "id": 1}])
            page.wait_for_timeout(30)
            if row_link.count() > 0:
                max_track = max(max_track, start_rel_x - _rel_x())
        _touch(cdp, "touchEnd", [])
        page.wait_for_timeout(600)

        # Any of these makes the swipe a live gesture: the row tracked the
        # finger, an archive/delete affordance appeared inside the row, the
        # swipe committed a session write, or the row left the sidebar
        # (action committed and the list updated).
        affordance = row_li.get_by_role(
            "button", name=re.compile(r"archive|delete", re.I)
        )
        affordance_visible = affordance.count() > 0 and affordance.first.is_visible()
        row_gone = row_link.count() == 0
        tracked = max_track >= _MIN_TRACK_PX

        assert tracked or affordance_visible or row_gone or session_writes, (
            f"swiping left {_SWIPE_PX}px across the session row did nothing: "
            f"the row tracked the finger {max_track:.0f}px (needs >= "
            f"{_MIN_TRACK_PX}px to count), no archive/delete affordance "
            f"appeared, and no session write fired (writes: {session_writes}); "
            "session rows have no swipe affordances"
        )
    finally:
        context.close()


def test_session_row_long_press_with_wobble_opens_menu(
    browser: Browser,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """A realistic long-press on a session row must open its actions menu.

    Journey: open session A at a phone viewport -> open the sidebar drawer ->
    long-press session B's row, holding ~0.9s with a couple px of natural
    finger wobble -> the row's actions menu (Archive/Delete/...) opens, and
    the press does NOT navigate.

    Failure mode this catches: competing gesture owners make
    the long-press flaky -- any pointer movement cancels the menu's
    long-press timer, so a real finger (which always wobbles) never opens the
    menu, and the synthesized click that trails the hold navigates to the
    pressed session instead.

    :param browser: Playwright browser to open the touch phone context on.
    :param seeded_session_pair: ``(base_url, session_a, session_b)`` --
        two runner-bound sessions in the same server, so a wrongful
        navigation is observable as a URL change from A to B.
    """
    base_url, session_a, session_b = seeded_session_pair
    _title_session(base_url, session_b, "Roadmap sync notes")

    context = _phone_context(browser)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_a}")
        expect(page.get_by_role("button", name="Open sidebar")).to_be_visible(timeout=30_000)
        _open_sidebar_drawer(page)

        row_b = page.locator(f'a[href="/c/{session_b}"]')
        expect(row_b).to_be_visible(timeout=30_000)
        box = row_b.bounding_box()
        assert box is not None
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2

        # Finger down on row B, hold ~0.9s wobbling +-2px, lift.
        cdp = context.new_cdp_session(page)
        _touch(cdp, "touchStart", [{"x": x, "y": y, "id": 1}])
        deadline = time.monotonic() + _HOLD_SECONDS
        flip = 1
        while time.monotonic() < deadline:
            _touch(cdp, "touchMove", [{"x": x + flip * _WOBBLE_PX, "y": y, "id": 1}])
            flip = -flip
            page.wait_for_timeout(90)
        _touch(cdp, "touchEnd", [])
        page.wait_for_timeout(500)

        menu_archive = page.get_by_test_id("archive-conversation")
        menu_open = menu_archive.count() > 0 and menu_archive.first.is_visible()
        navigated = f"/c/{session_b}" in page.url

        assert menu_open, (
            "long-pressing the session row (0.9s hold with a +-2px finger "
            "wobble) never opened the row's actions menu; instead the press "
            + (
                f"navigated to the pressed session ({page.url})"
                if navigated
                else f"did nothing (still on {page.url})"
            )
        )
        assert not navigated, (
            "the long-press must arm the actions menu, not navigate; the app "
            f"switched sessions to {page.url}"
        )
    finally:
        context.close()
