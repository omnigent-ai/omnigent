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

_SWIPE_ACTIONS_KEY = "omnigent:swipe-actions"


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


def _swipe(page: Page, row: Locator, delta_x: int) -> None:
    """Dispatch a partial touch gesture through the rendered row's handlers."""
    box = row.bounding_box()
    assert box is not None
    start_x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    row.evaluate(
        """(element, gesture) => {
            let captured = false;
            element.setPointerCapture = () => { captured = true; };
            element.hasPointerCapture = () => captured;
            element.releasePointerCapture = () => { captured = false; };
            const init = {
                bubbles: true,
                cancelable: true,
                pointerId: 1,
                pointerType: "touch",
                isPrimary: true,
                button: 0,
                buttons: 1,
                clientY: gesture.y,
            };
            element.dispatchEvent(new PointerEvent("pointerdown", {
                ...init,
                clientX: gesture.startX,
            }));
            element.dispatchEvent(new PointerEvent("pointermove", {
                ...init,
                clientX: gesture.startX + gesture.deltaX,
            }));
        }""",
        {"startX": start_x, "y": y, "deltaX": delta_x},
    )
    page.wait_for_timeout(250)


def _horizontal_edges(locator: Locator) -> dict[str, float]:
    """Read the browser's actual border-box edges, independent of CSS classes."""
    return locator.evaluate(
        """element => {
            const rect = element.getBoundingClientRect();
            const pixel = value => Math.round(value * 1000) / 1000;
            return {left: pixel(rect.left), right: pixel(rect.right)};
        }"""
    )


def _horizontal_margins(locator: Locator) -> dict[str, float]:
    """Read the browser-computed containing-block inset."""
    return locator.evaluate(
        """element => {
            const style = getComputedStyle(element);
            return {left: parseFloat(style.marginLeft), right: parseFloat(style.marginRight)};
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


def test_partial_swipe_reveal_is_adjacent_to_ellipsis_surface(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Real layout keeps left/delete and right/archive reveals gapless and disjoint."""
    base_url, session_id = seeded_session
    title = f"e2e-swipe-geometry-{uuid.uuid4().hex}-with-a-title-that-must-ellipsis"
    _set_title(base_url, session_id, title)
    page.add_init_script(
        f"""Object.defineProperty(navigator, "maxTouchPoints", {{value: 1}});
        localStorage.setItem(
            "{_SWIPE_ACTIONS_KEY}",
            JSON.stringify({{left: "delete", right: "archive"}}),
        )"""
    )

    for width in (390, 700):
        for delta_x, icon_class in ((-40, "lucide-trash-2"), (40, "lucide-archive")):
            page.set_viewport_size({"width": width, "height": 800})
            page.goto(f"{base_url}/c/{session_id}")
            open_sidebar = page.get_by_role("button", name="Open sidebar")
            if open_sidebar.is_visible():
                open_sidebar.click()
                page.wait_for_timeout(300)
            row = _row(page, session_id)
            link = row.locator(f'a[href="/c/{session_id}"]')
            expect(link).to_be_visible(timeout=30_000)
            resting_edges = _horizontal_edges(row)

            _swipe(page, row, delta_x)
            reveal = row.get_by_test_id("conversation-swipe-reveal")
            surface = row.get_by_test_id("conversation-swipe-surface")
            expect(reveal).to_be_visible()
            expect(reveal.locator(f"svg.{icon_class}")).to_have_count(1)
            expect(surface.get_by_test_id("conversation-actions")).to_have_count(1)

            title_span = link.locator("span.truncate").first
            assert title_span.evaluate("element => element.scrollWidth > element.clientWidth")

            reveal_edges = _horizontal_edges(reveal)
            surface_edges = _horizontal_edges(surface)
            swiping_edges = _horizontal_edges(row)
            assert _horizontal_margins(row) == {"left": 4, "right": 4}
            assert swiping_edges["left"] == resting_edges["left"] + 4
            assert swiping_edges["right"] == resting_edges["right"] - 4
            if delta_x < 0:
                assert surface_edges["right"] <= reveal_edges["left"]
            else:
                assert reveal_edges["right"] <= surface_edges["left"]
