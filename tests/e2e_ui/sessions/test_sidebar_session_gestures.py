"""Browser e2e coverage for touch gestures on draggable session rows.

The sidebar uses dnd-kit for touch dragging and Radix for its context menu.
These tests drive Chromium through CDP so the page receives a genuine touch
sequence; synthetic pointer events do not arm dnd-kit's ``TouchSensor``.
"""

from __future__ import annotations

import re
import uuid

import httpx
from playwright.sync_api import Browser, Locator, Page, expect


_MOBILE_VIEWPORT = {"width": 390, "height": 844}


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give the test session a unique, visible sidebar label."""
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    response.raise_for_status()


def _create_project(base_url: str, name: str) -> None:
    """Create an empty project for a drag target."""
    response = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    response.raise_for_status()


def _row_link(page: Page, session_id: str) -> Locator:
    """Locate the sidebar link for ``session_id``."""
    return page.locator(f'a[href="/c/{session_id}"]')


def _section(page: Page, title: str) -> Locator:
    """Locate the sidebar section headed by ``title``."""
    return page.locator("section").filter(
        has=page.get_by_role("button", name=title, exact=True)
    )


def test_still_touch_opens_session_context_menu_without_dragging(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A still touch opens the context menu; only a moving finger drags."""
    base_url, session_id = seeded_session
    title = f"e2e-touch-hold-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)

    context = browser.new_context(
        has_touch=True,
        is_mobile=True,
        viewport=_MOBILE_VIEWPORT,
    )
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
            cdp.send(
                "Input.dispatchTouchEvent",
                {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
            )

            # The hold arms and lifts the row; the menu itself waits for release,
            # so a finger that never moves is never picked up as a drag.
            expect(row).to_have_class(re.compile(r"\bscale-\[1\.01\]"), timeout=2000)
            expect(row).not_to_have_class(re.compile(r"\bopacity-40\b"))

            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            expect(page.get_by_test_id("rename-conversation")).to_have_count(1, timeout=2000)
            expect(row).not_to_have_class(re.compile(r"\bopacity-40\b"))
        finally:
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            cdp.detach()
    finally:
        context.close()


def test_vertical_touch_scroll_still_works_on_session_row(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Moving before the hold delay scrolls the sidebar instead of dragging."""
    base_url, session_id = seeded_session
    _set_title(base_url, session_id, f"e2e-touch-scroll-{uuid.uuid4().hex[:8]}")

    context = browser.new_context(
        has_touch=True,
        is_mobile=True,
        viewport=_MOBILE_VIEWPORT,
    )
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
            cdp.send(
                "Input.dispatchTouchEvent",
                {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
            )
            for offset in (20, 45, 70, 95, 120):
                cdp.send(
                    "Input.dispatchTouchEvent",
                    {"type": "touchMove", "touchPoints": [{"x": x, "y": y - offset}]},
                )
                page.wait_for_timeout(20)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        finally:
            cdp.detach()

        page.wait_for_function(
            "element => element.scrollTop > 20",
            arg=scroll_area.element_handle(),
        )
        assert "opacity-40" not in (row.get_attribute("class") or "")
        expect(page.get_by_test_id("rename-conversation")).to_have_count(0)
    finally:
        context.close()


def test_touch_drag_moves_session_into_project(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A touch drag still drops the session into a project folder."""
    base_url, session_id = seeded_session
    _set_title(base_url, session_id, f"e2e-touch-drop-{uuid.uuid4().hex[:8]}")
    project = f"Project {uuid.uuid4().hex[:6]}"
    _create_project(base_url, project)

    context = browser.new_context(
        has_touch=True,
        is_mobile=True,
        viewport=_MOBILE_VIEWPORT,
    )
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
            cdp.send(
                "Input.dispatchTouchEvent",
                {
                    "type": "touchStart",
                    "touchPoints": [{"x": start_x, "y": start_y}],
                },
            )
            # Hold until the row lifts, then the first move picks it up as a drag.
            expect(row).to_have_class(re.compile(r"\bscale-\[1\.01\]"), timeout=2000)

            for step in range(1, 6):
                progress = step / 5
                cdp.send(
                    "Input.dispatchTouchEvent",
                    {
                        "type": "touchMove",
                        "touchPoints": [
                            {
                                "x": start_x + (end_x - start_x) * progress,
                                "y": start_y + (end_y - start_y) * progress,
                            }
                        ],
                    },
                )
                page.wait_for_timeout(20)
                if step == 1:
                    # Moving out of the armed hold is what hands the row to dnd-kit.
                    expect(row).to_have_class(re.compile(r"\bopacity-40\b"), timeout=1000)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        finally:
            cdp.detach()

        expect(_section(page, project).locator(f'a[href="/c/{session_id}"]')).to_be_visible()
        expect(_section(page, "Sessions").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)
    finally:
        context.close()
