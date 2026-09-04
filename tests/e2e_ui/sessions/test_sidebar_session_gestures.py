"""Browser e2e coverage for touch gestures on draggable session rows.

The sidebar uses dnd-kit for touch dragging and Radix for its context menu.
These tests drive Chromium through CDP so the page receives a genuine touch
sequence; synthetic pointer events do not arm dnd-kit's ``TouchSensor``.
"""

from __future__ import annotations

import re
import time
import uuid

import httpx
from playwright.sync_api import Browser, BrowserContext, Locator, Page, expect

from tests.e2e_ui._touch import center, new_touch_context, touch
from tests.e2e_ui.conftest import set_session_title

_MOBILE_VIEWPORT = {"width": 390, "height": 844}
_UNEXPECTED_EVENT_SCRIPT = """
window.__rowGestureUnexpected = [];
window.__rowGestureCaptureEvents = [];
window.__rowGesturePointerId = null;
document.addEventListener('pointerdown', event => {
  window.__rowGesturePointerId = event.pointerId;
}, true);
document.addEventListener('lostpointercapture', event => {
  window.__rowGestureCaptureEvents.push({
    pointerId: event.pointerId,
    target: event.target.tagName,
    trusted: event.isTrusted,
  });
}, true);
for (const type of ['dragstart', 'pointercancel']) {
  document.addEventListener(
    type,
    () => window.__rowGestureUnexpected.push(type),
    true,
  );
}
"""


def _new_touch_context(browser: Browser) -> BrowserContext:
    context = new_touch_context(browser, is_mobile=True, viewport=_MOBILE_VIEWPORT)
    context.add_init_script(_UNEXPECTED_EVENT_SCRIPT)
    return context


def _unexpected_events(page: Page) -> list[str]:
    return page.evaluate("window.__rowGestureUnexpected")


def _create_project(base_url: str, name: str) -> None:
    """Create an empty project for a drag target."""
    response = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    response.raise_for_status()


def _row_link(page: Page, session_id: str) -> Locator:
    """Locate the sidebar link for ``session_id``."""
    return page.locator(f'a[href="/c/{session_id}"]')


def _section(page: Page, title: str) -> Locator:
    """Locate the sidebar section headed by ``title``."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def test_released_swipe_archives_without_navigating_and_stationary_tap_navigates(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A real released swipe commits archive, while a stationary touch remains a tap."""
    base_url, session_id = seeded_session
    set_session_title(base_url, session_id, f"e2e-touch-archive-{uuid.uuid4().hex[:8]}")

    context = _new_touch_context(browser)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/?sidebar=open")
        link = _row_link(page, session_id)
        expect(link).to_be_visible()
        x, y = center(link)

        cdp = page.context.new_cdp_session(page)
        try:
            touch(cdp, "touchStart", x, y)
            touch(cdp, "touchEnd")
            page.wait_for_url(f"**/c/{session_id}")

            page.goto(f"{base_url}/?sidebar=open")
            link = _row_link(page, session_id)
            expect(link).to_be_visible()
            row = link.locator("xpath=ancestor::li[1]")
            x, y = center(link)
            touch(cdp, "touchStart", x, y)
            for offset in (16, 32, 50, 74, 90):
                touch(cdp, "touchMove", x + offset, y)
                page.wait_for_timeout(25)
            expect(row).to_have_class(re.compile(r"\bmx-1\b"))
            assert _unexpected_events(page) == []
            touch(cdp, "touchEnd")
        finally:
            cdp.detach()

        deadline = time.monotonic() + 15.0
        archived = False
        while time.monotonic() < deadline:
            response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
            if response.status_code == 200 and response.json().get("archived") is True:
                archived = True
                break
            time.sleep(0.25)
        assert archived, (
            "released swipe should archive the session on the server; "
            f"events={_unexpected_events(page)} path={page.evaluate('location.pathname')}"
        )
        assert page.evaluate("location.pathname") == "/"
        assert _unexpected_events(page) == []
    finally:
        httpx.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"archived": False},
            timeout=10.0,
        ).raise_for_status()
        context.close()


def test_still_touch_opens_session_context_menu_without_dragging(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A still touch opens the context menu; only a moving finger drags."""
    base_url, session_id = seeded_session
    title = f"e2e-touch-hold-{uuid.uuid4().hex[:8]}"
    set_session_title(base_url, session_id, title)

    context = _new_touch_context(browser)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")

        link = _row_link(page, session_id)
        expect(link).to_be_visible()
        row = link.locator("xpath=ancestor::li[1]")
        box = link.bounding_box()
        assert box is not None, "session row has no touchable bounding box"
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2

        cdp = page.context.new_cdp_session(page)
        try:
            touch(cdp, "touchStart", x, y)

            # The hold arms, lifts the row, and opens exactly one menu without
            # starting either native link drag or dnd-kit drag.
            expect(row).to_have_class(re.compile(r"\bscale-\[1\.01\]"), timeout=2000)
            expect(row).not_to_have_class(re.compile(r"\bopacity-40\b"))
            expect(page.get_by_test_id("rename-conversation")).to_have_count(1)
            touch(cdp, "touchMove", x + 1, y)
            page.wait_for_timeout(100)
            expect(page.get_by_test_id("rename-conversation")).to_have_count(1)
            assert _unexpected_events(page) == []
            assert link.evaluate("element => element.draggable") is False

            touch(cdp, "touchEnd")
            expect(page.get_by_test_id("rename-conversation")).to_have_count(1, timeout=2000)
            expect(row).not_to_have_class(re.compile(r"\bopacity-40\b"))
            assert page.evaluate("window.__rowGestureUnexpected") == []
        finally:
            cdp.detach()
    finally:
        context.close()


def test_vertical_touch_scroll_still_works_on_session_row(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Moving before the hold delay scrolls the sidebar instead of dragging."""
    base_url, session_id = seeded_session
    set_session_title(base_url, session_id, f"e2e-touch-scroll-{uuid.uuid4().hex[:8]}")

    context = _new_touch_context(browser)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")

        link = _row_link(page, session_id)
        expect(link).to_be_visible()
        row = link.locator("xpath=ancestor::li[1]")
        scroll_area = page.locator("aside nav")
        conversation_list = page.get_by_test_id("sidebar-conversation-list")

        # One seeded row does not naturally overflow. Extend the real list's
        # layout so Chromium has native scroll range without adding fake rows.
        conversation_list.evaluate("element => { element.style.minHeight = '1800px'; }")
        scroll_area.evaluate("element => { element.scrollTop = 0; }")
        assert scroll_area.evaluate("element => element.scrollHeight > element.clientHeight")

        box = link.bounding_box()
        assert box is not None, "session row has no touchable bounding box"
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2

        cdp = page.context.new_cdp_session(page)
        try:
            touch(cdp, "touchStart", x, y)
            for offset in (20, 45, 70, 95, 120):
                touch(cdp, "touchMove", x, y - offset)
                page.wait_for_timeout(20)
            touch(cdp, "touchEnd")
        finally:
            cdp.detach()

        page.wait_for_function(
            "element => element.scrollTop > 20",
            arg=scroll_area.element_handle(),
        )
        assert "opacity-40" not in (row.get_attribute("class") or "")
        expect(page.get_by_test_id("rename-conversation")).to_have_count(0)
        events = _unexpected_events(page)
        assert "dragstart" not in events
        assert events in ([], ["pointercancel"])
    finally:
        context.close()


def test_horizontal_swipe_wins_before_hold_while_vertical_motion_yields_to_scroll(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Axis ownership resolves before the competing hold can arm."""
    base_url, session_id = seeded_session
    set_session_title(base_url, session_id, f"e2e-touch-axis-{uuid.uuid4().hex[:8]}")

    context = _new_touch_context(browser)
    try:
        page = context.new_page()
        page.clock.install()
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")
        link = _row_link(page, session_id)
        expect(link).to_be_visible()
        row = link.locator("xpath=ancestor::li[1]")
        x, y = center(link)
        # Freeze app timers without pausing the renderer that receives CDP input.
        pause_at = page.evaluate("new Date(Date.now() + 5000).toISOString()")
        page.clock.pause_at(pause_at)

        cdp = page.context.new_cdp_session(page)
        try:
            touch(cdp, "touchStart", x, y)
            page.clock.run_for(350)
            expect(page.get_by_test_id("rename-conversation")).to_have_count(0)
            expect(row).not_to_have_class(re.compile(r"\bscale-\[1\.01\]\b"))
            expect(row).not_to_have_class(re.compile(r"\bopacity-40\b"))
            touch(cdp, "touchMove", x - 11, y)
            page.clock.run_for(1)
            expect(row).not_to_have_class(re.compile(r"\bmx-1\b"))
            touch(cdp, "touchMove", x - 13, y)
            page.clock.run_for(1)
            expect(row).to_have_class(re.compile(r"\bmx-1\b"))
            page.clock.run_for(500)
            expect(page.get_by_test_id("rename-conversation")).to_have_count(0)
            expect(row).not_to_have_class(re.compile(r"\bscale-\[1\.01\]\b"))
            touch(cdp, "touchEnd")
        finally:
            cdp.detach()

        expect(row).not_to_have_class(re.compile(r"\bmx-1\b"))
        assert _unexpected_events(page) == []
    finally:
        context.close()


def test_pointercancel_resets_menu_and_drag_then_allows_another_hold(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Cancellation returns an armed or dragging row to a reusable idle state."""
    base_url, session_id = seeded_session
    set_session_title(base_url, session_id, f"e2e-touch-cancel-{uuid.uuid4().hex[:8]}")

    context = _new_touch_context(browser)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")
        link = _row_link(page, session_id)
        expect(link).to_be_visible()
        row = link.locator("xpath=ancestor::li[1]")
        x, y = center(link)

        cdp = page.context.new_cdp_session(page)
        try:
            touch(cdp, "touchStart", x, y)
            expect(page.get_by_test_id("rename-conversation")).to_have_count(1, timeout=2000)
            touch(cdp, "touchCancel")
            expect(page.get_by_test_id("rename-conversation")).to_have_count(0)
            expect(row).not_to_have_class(re.compile(r"\bscale-\[1\.01\]\b"))

            touch(cdp, "touchStart", x, y)
            expect(page.get_by_test_id("rename-conversation")).to_have_count(1, timeout=2000)
            touch(cdp, "touchMove", x, y + 12)
            expect(page.get_by_test_id("rename-conversation")).to_have_count(0)
            expect(row).to_have_class(re.compile(r"\bopacity-40\b"))
            touch(cdp, "touchCancel")
            expect(row).not_to_have_class(re.compile(r"\bopacity-40\b"))

            touch(cdp, "touchStart", x, y)
            expect(page.get_by_test_id("rename-conversation")).to_have_count(1, timeout=2000)
            touch(cdp, "touchEnd")
        finally:
            cdp.detach()

        assert _unexpected_events(page) == ["pointercancel", "pointercancel"]
    finally:
        context.close()


def test_lost_pointer_capture_resets_swipe_and_allows_another_swipe(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Unexpected capture loss clears translation without poisoning the next touch."""
    base_url, session_id = seeded_session
    set_session_title(base_url, session_id, f"e2e-touch-capture-{uuid.uuid4().hex[:8]}")

    context = _new_touch_context(browser)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")
        link = _row_link(page, session_id)
        expect(link).to_be_visible()
        row = link.locator("xpath=ancestor::li[1]")
        x, y = center(link)

        cdp = page.context.new_cdp_session(page)
        try:
            touch(cdp, "touchStart", x, y)
            touch(cdp, "touchMove", x - 40, y)
            expect(row).to_have_class(re.compile(r"\bmx-1\b"))
            touch(cdp, "touchMove", x - 41, y)
            page.wait_for_function("window.__rowGestureCaptureEvents.length > 0")
            assert page.evaluate("window.__rowGestureCaptureEvents")[0]["target"] == "SPAN"
            capture = row.evaluate(
                """element => {
                    const pointerId = window.__rowGesturePointerId;
                    const link = element.querySelector('a');
                    const state = {
                      pointerId,
                      row: element.hasPointerCapture(pointerId),
                      link: link.hasPointerCapture(pointerId),
                    };
                    if (state.row) element.releasePointerCapture(pointerId);
                    else if (state.link) link.releasePointerCapture(pointerId);
                    return state;
                }"""
            )
            assert capture["row"] is True, capture
            touch(cdp, "touchMove", x - 42, y)
            page.wait_for_function("window.__rowGestureCaptureEvents.length > 1")
            capture_events = page.evaluate("window.__rowGestureCaptureEvents")
            assert capture_events[1] == {
                "pointerId": capture["pointerId"],
                "target": "LI",
                "trusted": True,
            }
            expect(row).not_to_have_class(re.compile(r"\bmx-1\b"))
            touch(cdp, "touchEnd")

            touch(cdp, "touchStart", x, y)
            touch(cdp, "touchMove", x - 40, y)
            expect(row).to_have_class(re.compile(r"\bmx-1\b"))
            touch(cdp, "touchEnd")
            expect(row).not_to_have_class(re.compile(r"\bmx-1\b"))
        finally:
            cdp.detach()

        assert _unexpected_events(page) == []
    finally:
        context.close()


def test_touch_drag_moves_session_into_project(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A touch drag still drops the session into a project folder."""
    base_url, session_id = seeded_session
    set_session_title(base_url, session_id, f"e2e-touch-drop-{uuid.uuid4().hex[:8]}")
    project = f"Project {uuid.uuid4().hex[:6]}"
    _create_project(base_url, project)

    context = _new_touch_context(browser)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")

        link = _row_link(page, session_id)
        header = page.get_by_role("button", name=project, exact=True)
        expect(link).to_be_visible()
        expect(header).to_be_visible()
        row = link.locator("xpath=ancestor::li[1]")

        source = link.bounding_box()
        target = header.bounding_box()
        assert source is not None, "session row has no touchable bounding box"
        assert target is not None, "project header has no droppable bounding box"
        start_x = source["x"] + source["width"] / 2
        start_y = source["y"] + source["height"] / 2
        end_x = target["x"] + target["width"] / 2
        end_y = target["y"] + target["height"] / 2

        cdp = page.context.new_cdp_session(page)
        try:
            touch(cdp, "touchStart", start_x, start_y)
            page.wait_for_timeout(200)
            touch(cdp, "touchMove", start_x, start_y + 8)
            expect(page.get_by_test_id("rename-conversation")).to_have_count(0)
            # Hold until the row lifts, then the first move picks it up as a drag.
            expect(row).to_have_class(re.compile(r"\bscale-\[1\.01\]"), timeout=2000)
            expect(page.get_by_test_id("rename-conversation")).to_have_count(1)

            direction = 1 if end_y >= start_y else -1
            touch(cdp, "touchMove", start_x, start_y + 8 + direction * 9)
            expect(page.get_by_test_id("rename-conversation")).to_have_count(1)
            expect(row).not_to_have_class(re.compile(r"\bopacity-40\b"))

            # The first move across the threshold must both dismiss the menu
            # and reach dnd-kit on this same pointer.
            pull_y = start_y + 8 + direction * 11
            touch(cdp, "touchMove", start_x, pull_y)
            expect(page.get_by_test_id("rename-conversation")).to_have_count(0)
            expect(row).to_have_class(re.compile(r"\bopacity-40\b"), timeout=1000)

            for step in range(1, 6):
                progress = step / 5
                touch(
                    cdp,
                    "touchMove",
                    start_x + (end_x - start_x) * progress,
                    start_y + (end_y - start_y) * progress,
                )
                page.wait_for_timeout(20)
            touch(cdp, "touchEnd")
        finally:
            cdp.detach()

        expect(_section(page, project).locator(f'a[href="/c/{session_id}"]')).to_be_visible()
        expect(_section(page, "Sessions").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)
        assert page.evaluate("window.__rowGestureUnexpected") == []
    finally:
        context.close()
