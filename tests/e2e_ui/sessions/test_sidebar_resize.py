"""E2E coverage for resizing the desktop Conversations sidebar."""

from __future__ import annotations

from playwright.sync_api import Page, expect

_CONVERSATIONS = 'aside[aria-label="Conversations"]'
_RESIZE_HANDLE = '[data-testid="sidebar-resize-handle"]'


def _width(page: Page) -> float:
    box = page.locator(_CONVERSATIONS).bounding_box()
    assert box is not None
    return box["width"]


def test_sidebar_resize_persists_without_annexing_chat_scroll(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1400, "height": 800})
    page.goto(f"{base_url}/c/{session_id}")

    sidebar = page.locator(_CONVERSATIONS)
    handle = page.locator(_RESIZE_HANDLE)
    expect(sidebar).to_be_visible(timeout=30_000)
    expect(handle).to_be_visible()

    initial_width = _width(page)
    handle_box = handle.bounding_box()
    assert handle_box is not None
    handle_x = handle_box["x"] + handle_box["width"] / 2
    handle_y = handle_box["y"] + handle_box["height"] / 2
    target_x = initial_width + 80

    # Playwright's mouse input emits the pointer events used by the resize hook.
    page.mouse.move(handle_x, handle_y)
    page.mouse.down()
    page.mouse.move(target_x, handle_y, steps=5)
    page.mouse.up()

    resized_width = _width(page)
    assert abs(resized_width - target_x) < 2, (resized_width, target_x)

    page.reload()
    expect(handle).to_be_visible(timeout=30_000)
    persisted_width = _width(page)
    assert abs(persisted_width - resized_width) < 2, (persisted_width, resized_width)

    # A scroll start beside the handle belongs to chat and must not resize.
    handle_box = handle.bounding_box()
    assert handle_box is not None
    # The seam handle reaches into chat for a usable hit target, so start just
    # beyond its actual border box.
    chat_x = handle_box["x"] + handle_box["width"] + 1
    scroll_y = handle_box["y"] + handle_box["height"] / 2
    page.mouse.move(chat_x, scroll_y)
    page.mouse.down()
    page.mouse.move(chat_x, scroll_y + 80, steps=5)
    page.mouse.up()

    assert abs(_width(page) - persisted_width) < 1, (_width(page), persisted_width)
