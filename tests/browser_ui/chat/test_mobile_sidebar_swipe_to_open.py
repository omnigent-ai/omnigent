"""Swiping right across the chat opens the mobile sidebar, as in the ChatGPT app.

The SPA runs as a plain touch browser (the Android shell has no native edge
swipe, since the system back gesture owns both screen edges), so every swipe is
a real CDP touch sequence. At wide-phone sizes the open sidebar sits beside the
chat instead of over it.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import CDPSession, Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract

_DRAWER = 'aside[aria-label="Conversations"]'
_COMPOSER = "Message the agent"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 390, "height": 844},
        "has_touch": True,
        "is_mobile": True,
    }


def _open_app(page: Page, chat: ChatSessionContract) -> CDPSession:
    page.goto(chat.url)
    expect(page.get_by_label(_COMPOSER)).to_be_visible()
    expect(page.locator(_DRAWER)).to_have_attribute("data-collapsed", "true")
    return page.context.new_cdp_session(page)


def _drawer_box(page: Page) -> dict[str, float]:
    return page.evaluate(
        f"""() => {{
          const r = document.querySelector('{_DRAWER}').getBoundingClientRect();
          return {{ left: r.left, right: r.right, width: r.width }};
        }}"""
    )


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
) -> dict[str, float]:
    """Drag one finger by (dx, dy); return the drawer's box just before lifting."""
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for step in range(1, steps + 1):
        point = {"x": x + dx * step / steps, "y": y + dy * step / steps}
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
        page.wait_for_timeout(step_ms)
    box_before_lift = _drawer_box(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    return box_before_lift


def test_swipe_right_opens_drawer(page: Page, chat_session_contract) -> None:
    """CUJ-1: a right swipe across the chat slides the drawer open under the finger."""
    chat = chat_session_contract
    chat.seed_sidebar_sessions([f"Swipe demo session {index:02d}" for index in range(1, 7)])
    cdp = _open_app(page, chat)
    composer = page.get_by_label(_COMPOSER)
    composer.focus()
    expect(composer).to_be_focused()

    box = _drag(cdp, page, x=80, y=420, dx=200, steps=16, step_ms=30)
    # Slow enough to open by distance (past a third of the drawer), not as a flick.
    expected_left = 200 - box["width"]
    assert abs(box["left"] - expected_left) < 30, f"the drawer did not track the finger: {box}"
    _expect_drawer_open(page)
    expect(page.locator(_DRAWER).get_by_text("Swipe demo session 01")).to_be_visible()
    # On iOS a still-focused composer keeps the soft keyboard up over the drawer.
    expect(composer).not_to_be_focused()
    assert page.url == chat.url

    # The existing left swipe still closes it.
    _drag(cdp, page, x=390 - 20, y=500, dx=-240, steps=16, step_ms=30)
    _expect_drawer_closed(page)


def test_short_vertical_and_edge_drags_leave_drawer_closed(
    page: Page, chat_session_contract
) -> None:
    """CUJ-2: a short drag, a scroll, or a swipe from the OS back-gesture edge does nothing."""
    chat = chat_session_contract
    cdp = _open_app(page, chat)

    box = _drag(cdp, page, x=80, y=420, dx=70, steps=7, step_ms=40)
    assert box["right"] > 30, f"the drawer did not follow the short drag: {box}"
    _expect_drawer_closed(page)

    _drag(cdp, page, x=200, y=600, dx=20, dy=-300, steps=14, step_ms=16)
    _expect_drawer_closed(page)

    box = _drag(cdp, page, x=8, y=420, dx=260, steps=16, step_ms=30)
    assert box["right"] < 1, f"an edge swipe moved the drawer: {box}"
    _expect_drawer_closed(page)


def test_wide_phone_docks_sidebar_beside_chat(page: Page, chat_session_contract) -> None:
    """CUJ-3: on an unfolded foldable the open sidebar pushes the chat instead of covering it."""
    chat = chat_session_contract
    page.set_viewport_size({"width": 720, "height": 760})
    cdp = _open_app(page, chat)
    composer = page.get_by_label(_COMPOSER)
    composer_left_closed = composer.bounding_box()["x"]  # type: ignore[index]

    _drag(cdp, page, x=200, y=420, dx=220, steps=16, step_ms=30)
    _expect_drawer_open(page)
    expect(page.get_by_test_id("sidebar-scrim")).to_have_count(0)
    page.wait_for_function(
        f"() => document.querySelector('{_DRAWER}').getBoundingClientRect().width > 300"
    )
    drawer = _drawer_box(page)
    page.wait_for_function(
        "([el, right]) => el.getBoundingClientRect().x >= right - 1",
        arg=[composer.element_handle(), drawer["right"]],
    )
    composer_box = composer.bounding_box()
    assert composer_box is not None
    assert composer_box["x"] >= drawer["right"] - 1, (
        f"the sidebar covers the chat: drawer={drawer} composer={composer_box}"
    )
    assert composer_box["x"] > composer_left_closed
    # The chat stays usable while the sidebar is docked.
    composer.tap()
    _expect_drawer_open(page)

    page.locator(_DRAWER).get_by_role("button", name="Close sidebar").tap()
    _expect_drawer_closed(page)
