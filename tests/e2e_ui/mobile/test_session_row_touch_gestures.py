"""Phone-viewport Chromium journeys for row swipes and wobbly long-presses.

CDP touch input includes the browser's pointer and synthesized-click events.
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
# Ignore subpixel jitter when checking whether the row follows a swipe.
_MIN_TRACK_PX = 24

# Past Radix's 700ms long-press threshold.
_HOLD_SECONDS = 0.9
# Realistic finger wobble while holding: a couple of CSS px side to side.
_WOBBLE_PX = 2


def _touch(cdp: CDPSession, type_: str, points: list[dict[str, float]]) -> None:
    """Dispatch one trusted touch event via CDP."""
    cdp.send("Input.dispatchTouchEvent", {"type": type_, "touchPoints": points})


def _phone_context(browser: Browser) -> BrowserContext:
    """Open a touch phone context, honoring the recording harness."""
    kwargs: dict = {"viewport": _VIEWPORT, "has_touch": True, "is_mobile": True}
    # The recording fixture patches only async Playwright; this sync context
    # reads the recording directory itself.
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
    """A left swipe must move the row, reveal an action, or commit a write."""
    base_url, session_id = seeded_session
    _title_session(base_url, session_id, "Weekly report draft")

    context = _phone_context(browser)
    try:
        page = context.new_page()

        # Record any archive/delete the swipe might commit.
        session_writes: list[str] = []

        def _on_request(request) -> None:
            if (
                request.method in ("PATCH", "DELETE")
                and f"/v1/sessions/{session_id}" in request.url
            ):
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

        # Sample row tracking during the swipe, before the release resets it.
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

        # Motion, a reveal, a write, or removal shows that the swipe was handled.
        affordance = row_li.get_by_role("button", name=re.compile(r"archive|delete", re.I))
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
    """A wobbly long-press opens actions without navigating to the pressed row."""
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

        # Hold on row B with a small finger wobble, then lift.
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
