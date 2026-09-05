"""Browser e2e coverage for compact sidebar session-row layout.

The sidebar keeps row actions absolutely positioned so hidden controls do not
consume title width. A desktop hover reveals those controls and reserves space
for them, while the session list remains scrollable without visible scrollbar
chrome. This test checks the real browser geometry that jsdom cannot measure.
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import set_session_title

_SWIPE_ACTIONS_KEY = "omnigent:swipe-actions"


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


def test_session_row_uses_full_title_width_until_actions_are_revealed(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The compact row expands its title by default and reserves hover actions."""
    base_url, session_id = seeded_session
    title = f"e2e-sidebar-row-layout-{uuid.uuid4().hex[:12]}-with-a-long-session-title"
    set_session_title(base_url, session_id, title)

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
    assert _padding(link) == {"left": 8, "right": 80}


@pytest.mark.parametrize("width", [390, 1280], ids=["narrow", "desktop"])
def test_session_row_keyboard_focus_outline_is_not_clipped(
    page: Page,
    seeded_session: tuple[str, str],
    width: int,
) -> None:
    """Keyboard focus outlines and control rings stay inside their clips."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(f"{base_url}/c/{session_id}?sidebar=open")
    link = _row(page, session_id).locator(f'a[href="/c/{session_id}"]')
    expect(link).to_be_visible()
    page.wait_for_function(
        """() => Math.abs(document.querySelector('aside[aria-label="Conversations"]')
            .getBoundingClientRect().left) < 0.01"""
    )
    for _ in range(60):
        page.keyboard.press("Tab")
        if link.evaluate("element => element === document.activeElement"):
            break
    expect(link).to_be_focused()
    outline = link.evaluate(
        """element => {
            const style = getComputedStyle(element);
            const width = parseFloat(style.outlineWidth);
            const offset = parseFloat(style.outlineOffset);
            const rect = element.getBoundingClientRect();
            const bounds = {
                left: rect.left - offset - width,
                right: rect.right + offset + width,
                top: rect.top - offset - width,
                bottom: rect.bottom + offset + width,
            };
            const clips = [];
            for (let ancestor = element.parentElement; ancestor;
                ancestor = ancestor.parentElement) {
                const overflow = getComputedStyle(ancestor);
                const box = ancestor.getBoundingClientRect();
                const left = box.left + ancestor.clientLeft;
                const top = box.top + ancestor.clientTop;
                if (overflow.overflowX !== 'visible') {
                    clips.push({axis: 'x', start: left, end: left + ancestor.clientWidth});
                }
                if (overflow.overflowY !== 'visible') {
                    clips.push({axis: 'y', start: top, end: top + ancestor.clientHeight});
                }
            }
            return {width, style: style.outlineStyle, color: style.outlineColor, bounds, clips};
        }"""
    )
    assert outline["width"] >= 2, outline
    assert outline["style"] not in ("none", "hidden"), outline
    assert outline["color"] not in ("transparent", "rgba(0, 0, 0, 0)"), outline
    assert outline["clips"], "the regression requires a clipping ancestor"
    for clip in outline["clips"]:
        start, end = ("left", "right") if clip["axis"] == "x" else ("top", "bottom")
        assert outline["bounds"][start] >= clip["start"] - 0.5, outline
        assert outline["bounds"][end] <= clip["end"] + 0.5, outline

    if width < 768:
        return
    control_rings = []
    for test_id in (
        "quick-pin-conversation",
        "quick-archive-conversation",
        "conversation-actions",
    ):
        page.keyboard.press("Tab")
        control = _row(page, session_id).get_by_test_id(test_id)
        expect(control).to_be_focused()
        expect(control).to_have_css("opacity", "1")
        ring = control.evaluate(
            r"""element => {
                const style = getComputedStyle(element);
                const shadows = style.boxShadow.split(/,(?![^()]*\))/);
                let extent = 0;
                let ringWidth = 0;
                for (const shadow of shadows) {
                    const [offsetX = 0, offsetY = 0, blur = 0, spread = 0] =
                        (shadow.match(/-?\d+(?:\.\d+)?px/g) || []).map(parseFloat);
                    ringWidth = Math.max(ringWidth, spread);
                    if (!shadow.includes('inset')) {
                        extent = Math.max(extent,
                            spread + blur + Math.max(Math.abs(offsetX), Math.abs(offsetY)));
                    }
                }
                const rect = element.getBoundingClientRect();
                const row = element.closest('li');
                const frame = row.getBoundingClientRect();
                const clip = {
                    left: frame.left + row.clientLeft,
                    top: frame.top + row.clientTop,
                    right: frame.left + row.clientLeft + row.clientWidth,
                    bottom: frame.top + row.clientTop + row.clientHeight,
                };
                const bounds = {left: rect.left - extent, right: rect.right + extent,
                    top: rect.top - extent, bottom: rect.bottom + extent};
                const clipped = ['left', 'top'].filter(edge => bounds[edge] < clip[edge] - 0.05)
                    .concat(['right', 'bottom'].filter(edge => bounds[edge] > clip[edge] + 0.05));
                return {control: element.dataset.testid, ringWidth, bounds, clip, clipped,
                    shadow: style.boxShadow};
            }"""
        )
        assert ring["ringWidth"] >= 3, ring
        control_rings.append(ring)
    assert all(not ring["clipped"] for ring in control_rings), control_rings


def test_partial_swipe_reveal_is_adjacent_to_ellipsis_surface(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Real layout keeps left/delete and right/archive reveals gapless and disjoint."""
    base_url, session_id = seeded_session
    title = f"e2e-swipe-geometry-{uuid.uuid4().hex}-" + "must-ellipsis-" * 10
    set_session_title(base_url, session_id, title)
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
            row = _row(page, session_id)
            link = row.locator(f'a[href="/c/{session_id}"]')
            expect(link).to_be_visible(timeout=30_000)
            expect(link).to_contain_text(title)
            page.wait_for_function(
                """() => Math.abs(document.querySelector('aside[aria-label="Conversations"]')
                    .getBoundingClientRect().left) < 0.01"""
            )
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
            assert swiping_edges == pytest.approx(resting_edges, abs=0.05)
            assert surface_edges["left"] == pytest.approx(
                resting_edges["left"] + delta_x, abs=0.05
            )
            assert surface_edges["right"] == pytest.approx(
                resting_edges["right"] + delta_x, abs=0.05
            )
            if delta_x < 0:
                assert surface_edges["right"] == reveal_edges["left"]
                assert reveal_edges["right"] == pytest.approx(resting_edges["right"], abs=0.05)
            else:
                assert reveal_edges["right"] == surface_edges["left"]
                assert reveal_edges["left"] == pytest.approx(resting_edges["left"], abs=0.05)
