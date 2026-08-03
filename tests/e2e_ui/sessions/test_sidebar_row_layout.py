"""Browser e2e coverage for compact sidebar session-row layout.

The sidebar keeps row actions absolutely positioned so hidden controls do not
consume title width. A desktop hover reveals those controls and reserves space
for them, while the session list remains scrollable without visible scrollbar
chrome. This test checks the real browser geometry that jsdom cannot measure.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


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
    expect(link).to_have_css("height", "32px")
    expect(link).to_contain_text(title)

    # Hidden desktop actions must not reserve width. The title surface starts
    # with equal 8px insets, matching the other sidebar rows.
    assert _padding(link) == {"left": 8, "right": 8}

    # The list still scrolls, but browser scrollbar chrome is intentionally
    # suppressed so it does not consume a large strip of sidebar width.
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
    assert scrollbar_width == "none"

    # Hovering reveals both actions and shortens only the right side of the
    # title surface. The tooltip carries metadata without adding a second row.
    row.hover()
    expect(row.get_by_test_id("quick-pin-conversation")).to_be_visible()
    expect(row.get_by_test_id("conversation-actions")).to_be_visible()
    expect(page.get_by_test_id("session-tooltip-content")).to_be_visible()
    assert _padding(link) == {"left": 8, "right": 56}
