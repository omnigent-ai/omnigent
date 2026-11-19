"""Browser e2e coverage for compact sidebar session-row layout.

The sidebar keeps row actions absolutely positioned so hidden controls do not
consume title width. A desktop hover reveals those controls and reserves space
for them, while the session list scrolls behind a thin, always-reserved
scrollbar gutter. This test checks the real browser geometry that jsdom cannot
measure.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
from playwright.sync_api import Browser, Locator, Page, expect

from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give the seeded session a unique, deliberately long title."""
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    response.raise_for_status()


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar list item for *session_id* by its stable href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _seed_titled_session(base_url: str, title: str) -> str:
    """Create an unfiled session titled *title* and return its id."""
    response = post_session_bundle(
        httpx.post, f"{base_url}/v1/sessions", _build_hello_world_bundle(), timeout=30.0
    )
    response.raise_for_status()
    session_id = response.json()["session_id"]
    _set_title(base_url, session_id, title)
    return session_id


def _list_overflows(page: Page) -> bool:
    """Return whether the sidebar's session list currently scrolls."""
    return page.locator('aside[aria-label="Conversations"] nav').evaluate(
        "nav => nav.scrollHeight > nav.clientHeight"
    )


def _section_width(page: Page, title: str) -> float:
    """Return the rendered width of the sidebar section headed *title*."""
    section = page.locator("section").filter(
        has=page.get_by_role("button", name=title, exact=True)
    )
    box = section.bounding_box()
    assert box is not None
    return box["width"]


def _padding(locator: Locator) -> dict[str, float]:
    """Return the link's computed horizontal padding in CSS pixels."""
    return locator.evaluate(
        """element => {
            const style = getComputedStyle(element);
            return {
                left: parseFloat(style.paddingLeft),
                right: parseFloat(style.paddingRight),
            };
        }"""
    )


def test_session_row_uses_full_title_width_until_actions_are_revealed(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The compact row expands its title by default and reserves hover actions."""
    base_url, session_id = seeded_session
    title = f"e2e-sidebar-row-layout-{uuid.uuid4().hex[:12]}-with-a-long-session-title"
    _set_title(base_url, session_id, title)

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    link = row.locator(f'a[href="/c/{session_id}"]')
    expect(link).to_be_visible(timeout=30_000)
    expect(link).to_have_css("height", "28px")
    expect(link).to_contain_text(title)

    # Hidden desktop actions must not reserve width. The title surface starts
    # with equal 8px insets, matching the other sidebar rows.
    assert _padding(link) == {"left": 8, "right": 8}

    # The list scrolls behind a thin, always-reserved gutter: narrow enough not
    # to claim a strip of sidebar width, and stable so the thumb cannot reflow rows.
    scrollbar_width = row.evaluate(
        """element => {
            let ancestor = element.parentElement;
            while (ancestor) {
                const style = getComputedStyle(ancestor);
                if (style.overflowY === "auto" || style.overflowY === "scroll") {
                    return style.scrollbarWidth;
                }
                ancestor = ancestor.parentElement;
            }
            return null;
        }"""
    )
    assert scrollbar_width == "thin"

    # Hovering reveals both actions and shortens only the right side of the
    # title surface. The tooltip carries metadata without adding a second row.
    row.hover()
    expect(row.get_by_test_id("quick-pin-conversation")).to_be_visible()
    expect(row.get_by_test_id("conversation-actions")).to_be_visible()
    expect(page.get_by_test_id("session-tooltip-content")).to_be_visible()
    assert _padding(link) == {"left": 8, "right": 80}


def test_collapsing_sessions_keeps_the_sidebar_row_width(
    launch_browser: Callable[..., Browser],
    live_server: str,
) -> None:
    """Toggling Sessions in and out of overflow leaves the other rows' width alone.

    The list's scrollbar gutter stays reserved when the content fits, so the
    scrollbar appearing or disappearing cannot reflow the rows.
    """
    prefix = f"e2e-sidebar-gutter-{uuid.uuid4().hex[:8]}"
    session_ids = [_seed_titled_session(live_server, f"{prefix}-{i:02d}") for i in range(30)]
    # Headless Chromium hides scrollbars by default; keep them so the gutter
    # takes real width, as it does in desktop browsers.
    browser = launch_browser(ignore_default_args=["--hide-scrollbars"])
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(f"{live_server}/")
        sessions = page.get_by_role("button", name="Sessions", exact=True)
        expect(page.get_by_role("link", name=f"{prefix}-00")).to_be_visible(timeout=30_000)
        assert _list_overflows(page)
        expanded_width = _section_width(page, "Projects")

        sessions.click()
        expect(sessions).to_have_attribute("aria-expanded", "false")
        assert not _list_overflows(page)
        assert _section_width(page, "Projects") == expanded_width

        sessions.click()
        expect(sessions).to_have_attribute("aria-expanded", "true")
        assert _list_overflows(page)
        assert _section_width(page, "Projects") == expanded_width
    finally:
        browser.close()
        for session_id in session_ids:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
