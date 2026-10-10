"""Browser e2e: the chat header's session title must center on the chat pane.

Journey from the report: open a titled session at desktop width, then drag the
Conversations sidebar's right-edge handle to widen it. The title should stay
centered over the chat pane and clear of the sidebar; a left-anchored header
slot instead drags it along with the sidebar's edge.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

_CONVERSATIONS = 'aside[aria-label="Conversations"]'
_TITLE = "Header centering check"
_VIEWPORT = {"width": 1440, "height": 900}
_SIDEBAR_TARGET_PX = 640
# Well under the left-anchored breadcrumb's offset (hundreds of px on a wide
# pane) while leaving room for sub-pixel rounding and rename-control padding.
_CENTER_TOLERANCE_PX = 40
_LAYOUT_SETTLE_MS = 300


def _set_title(base_url: str, session_id: str, title: str) -> None:
    resp = httpx.patch(f"{base_url}/v1/sessions/{session_id}", json={"title": title}, timeout=10.0)
    resp.raise_for_status()


def _center_x(box: dict[str, float]) -> float:
    return box["x"] + box["width"] / 2


def _drag_sidebar_edge_to(page: Page, target_x: int) -> None:
    handle = page.get_by_role("separator", name="Resize sidebar")
    expect(handle).to_be_visible()
    box = handle.bounding_box()
    assert box is not None
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] / 2, y)
    page.mouse.down()
    page.mouse.move(target_x, y, steps=16)
    page.mouse.up()
    page.wait_for_function(
        """([selector, target]) => {
            const el = document.querySelector(selector);
            return el && Math.abs(el.getBoundingClientRect().width - target) < 4;
        }""",
        arg=[_CONVERSATIONS, target_x],
        timeout=5_000,
    )
    page.wait_for_timeout(_LAYOUT_SETTLE_MS)


@pytest.mark.browser_context_args(viewport=_VIEWPORT, record_video_size=_VIEWPORT)
def test_header_title_stays_centered_on_chat_pane_when_sidebar_widens(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    _set_title(base_url, session_id, _TITLE)

    # Requested after the non-browser setup so a recording starts on the journey.
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    title = page.get_by_test_id("header-title")
    expect(title).to_be_visible(timeout=30_000)
    expect(title).to_have_text(_TITLE)
    sidebar = page.locator(_CONVERSATIONS)
    expect(sidebar).not_to_have_attribute("data-collapsed", "true")
    pane = page.get_by_role("main")
    expect(pane).to_be_visible()
    page.wait_for_timeout(_LAYOUT_SETTLE_MS)

    before = title.bounding_box()
    pane_before = pane.bounding_box()
    assert before is not None and pane_before is not None
    offset_before = _center_x(before) - _center_x(pane_before)

    _drag_sidebar_edge_to(page, _SIDEBAR_TARGET_PX)

    sidebar_box = sidebar.bounding_box()
    pane_box = pane.bounding_box()
    title_box = title.bounding_box()
    assert sidebar_box is not None and pane_box is not None and title_box is not None
    sidebar_right = sidebar_box["x"] + sidebar_box["width"]
    assert abs(pane_box["x"] - sidebar_right) <= 2, (pane_box, sidebar_box)

    assert title_box["x"] >= sidebar_right - 1, (
        f"sidebar overlaps the title: title starts at {title_box['x']:.0f}px, "
        f"sidebar's right edge is {sidebar_right:.0f}px"
    )

    offset = _center_x(title_box) - _center_x(pane_box)
    assert abs(offset) <= _CENTER_TOLERANCE_PX, (
        f"header title is not centered on the chat pane after widening the sidebar to "
        f"{sidebar_box['width']:.0f}px: title midpoint {_center_x(title_box):.0f}px vs pane "
        f"midpoint {_center_x(pane_box):.0f}px (offset {offset:+.0f}px, tolerance "
        f"{_CENTER_TOLERANCE_PX}px; title left edge sits {title_box['x'] - sidebar_right:.0f}px "
        f"from the sidebar edge; offset before widening was {offset_before:+.0f}px)"
    )


_LONG_TITLE = (
    "Investigate why the nightly release pipeline keeps timing out while publishing "
    "the desktop installers, then draft a fix and a rollout plan for the team"
)
_NARROW_VIEWPORT = {"width": 960, "height": 800}
_NARROW_SIDEBAR_PX = 480  # the sidebar's 50%-of-viewport ceiling at this width
# The breadcrumb is one centered grid item, so its midpoint lands on the pane's
# midpoint up to sub-pixel rounding; the title inside it may sit off-center.
_BREADCRUMB_TOLERANCE_PX = 4


def _open_titled_session(page: Page, base_url: str, session_id: str, title: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    header_title = page.get_by_test_id("header-title")
    expect(header_title).to_be_visible(timeout=30_000)
    expect(header_title).to_have_text(title)
    expect(page.locator(_CONVERSATIONS)).not_to_have_attribute("data-collapsed", "true")
    expect(page.get_by_role("main")).to_be_visible()
    page.wait_for_timeout(_LAYOUT_SETTLE_MS)


def _close_workspace_panel(page: Page) -> None:
    """Keep the chat pane at its full width; the panel's default state varies."""
    collapse = page.get_by_role("button", name="Collapse right panel")
    if collapse.count() and collapse.first.is_visible():
        collapse.first.click()
        page.wait_for_timeout(_LAYOUT_SETTLE_MS)


@pytest.mark.browser_context_args(viewport=_VIEWPORT, record_video_size=_VIEWPORT)
def test_long_header_title_truncates_centered_inside_the_chat_pane(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """A long title truncates centered in the pane, clear of the sidebar and the
    header actions, even though only the header's right side holds controls."""
    base_url, session_id = seeded_session
    _set_title(base_url, session_id, _LONG_TITLE)

    page: Page = request.getfixturevalue("page")
    _open_titled_session(page, base_url, session_id, _LONG_TITLE)
    _drag_sidebar_edge_to(page, _SIDEBAR_TARGET_PX)

    sidebar_box = page.locator(_CONVERSATIONS).bounding_box()
    pane_box = page.get_by_role("main").bounding_box()
    title = page.get_by_test_id("header-title")
    title_box = title.bounding_box()
    crumb_box = page.get_by_role("navigation", name="Conversation").bounding_box()
    actions_box = page.get_by_test_id("header-actions").bounding_box()
    assert sidebar_box is not None and pane_box is not None
    assert title_box is not None and crumb_box is not None and actions_box is not None
    sidebar_right = sidebar_box["x"] + sidebar_box["width"]
    pane_right = pane_box["x"] + pane_box["width"]
    title_right = title_box["x"] + title_box["width"]

    assert title.evaluate("el => el.scrollWidth > el.clientWidth"), (
        f"expected the {len(_LONG_TITLE)}-character title to be truncated at "
        f"{title_box['width']:.0f}px; widen the title if the header got roomier"
    )
    assert title_box["x"] >= sidebar_right - 1 and title_right <= pane_right + 1, (
        f"title spills outside the chat pane: title spans "
        f"{title_box['x']:.0f}..{title_right:.0f}px, "
        f"pane spans {pane_box['x']:.0f}..{pane_right:.0f}px"
    )
    assert title_right <= actions_box["x"] + 1, (
        f"title runs under the header actions: title ends at {title_right:.0f}px, "
        f"actions start at {actions_box['x']:.0f}px"
    )
    offset = _center_x(crumb_box) - _center_x(pane_box)
    assert abs(offset) <= _BREADCRUMB_TOLERANCE_PX, (
        f"truncated breadcrumb is not centered on the chat pane: breadcrumb midpoint "
        f"{_center_x(crumb_box):.0f}px vs pane midpoint {_center_x(pane_box):.0f}px "
        f"(offset {offset:+.0f}px, tolerance {_BREADCRUMB_TOLERANCE_PX}px)"
    )


@pytest.mark.browser_context_args(viewport=_NARROW_VIEWPORT, record_video_size=_NARROW_VIEWPORT)
def test_narrow_chat_pane_keeps_the_title_visible_and_the_controls_inside(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """At a 480px pane the title stays readable and centered, and every header
    control stays inside the pane instead of overflowing past its right edge."""
    base_url, session_id = seeded_session
    _set_title(base_url, session_id, _TITLE)

    page: Page = request.getfixturevalue("page")
    _open_titled_session(page, base_url, session_id, _TITLE)
    _close_workspace_panel(page)
    _drag_sidebar_edge_to(page, _NARROW_SIDEBAR_PX)

    pane_box = page.get_by_role("main").bounding_box()
    title_box = page.get_by_test_id("header-title").bounding_box()
    crumb_box = page.get_by_role("navigation", name="Conversation").bounding_box()
    actions = page.get_by_test_id("header-actions")
    actions_box = actions.bounding_box()
    assert pane_box is not None and title_box is not None
    assert crumb_box is not None and actions_box is not None
    pane_right = pane_box["x"] + pane_box["width"]
    assert pane_box["width"] <= _NARROW_SIDEBAR_PX + 2, pane_box

    assert title_box["width"] >= 40, (
        f"title collapsed to {title_box['width']:.0f}px inside a {pane_box['width']:.0f}px pane"
    )
    assert crumb_box["x"] >= pane_box["x"] - 1, (crumb_box, pane_box)
    assert crumb_box["x"] + crumb_box["width"] <= actions_box["x"] + 1, (crumb_box, actions_box)
    assert actions_box["x"] + actions_box["width"] <= pane_right + 1, (
        f"header actions overflow the pane: actions end at "
        f"{actions_box['x'] + actions_box['width']:.0f}px, pane ends at {pane_right:.0f}px"
    )
    for control in actions.get_by_role("button").all():
        box = control.bounding_box()
        assert box is not None and box["x"] + box["width"] <= pane_right + 1, (
            f"{control.get_attribute('aria-label')} sits outside the pane: {box}"
        )
    offset = _center_x(crumb_box) - _center_x(pane_box)
    assert abs(offset) <= _BREADCRUMB_TOLERANCE_PX, (
        f"breadcrumb is not centered in the narrow pane: midpoint {_center_x(crumb_box):.0f}px "
        f"vs pane midpoint {_center_x(pane_box):.0f}px (offset {offset:+.0f}px)"
    )
