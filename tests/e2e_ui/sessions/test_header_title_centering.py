"""E2E: the header session title centers on the chat pane.

The chat header renders the session title inside its *left* slot (the
breadcrumb in ``ChatHeader``), so the title hugs the sidebar's right edge.
When a user widens the Conversations sidebar — increasingly common now that
the sidebar previews code/PDFs/websites — the title rides along with the
sidebar's edge, sitting far left of the chat pane's midpoint and reading as
overlapped/attached to the sidebar instead of belonging to the chat.

Journey (from the report):

1. open a session in the web/desktop UI (same SPA header on both)
2. drag the sidebar's right-edge resize handle to widen the sidebar
3. the session title should sit centered on the chat pane — the region
   between the sidebar's right edge and the Workspace rail's left edge —
   and must never sit underneath the sidebar

Pure client-side layout, so no LLM turn is needed. The viewport is pinned to
1600px so the sidebar can be dragged to 640px (its ceiling is half the
viewport) while the chat pane keeps its 480px minimum without squeezing the
Workspace rail.
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect

_CONVERSATIONS = 'aside[aria-label="Conversations"]'
# A centered title lands on the pane's midpoint; the buggy left-slot layout
# leaves it hundreds of px to the left, so the tolerance only absorbs
# rounding and the title's own truncation, never the failure mode.
_CENTER_TOLERANCE_PX = 60
_SIDEBAR_TARGET_PX = 640


def _box(locator: Locator) -> dict[str, float]:
    box = locator.bounding_box()
    assert box is not None, f"no bounding box for {locator}"
    return box


def test_header_title_centers_on_chat_pane_when_sidebar_widens(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Widening the sidebar must leave the title centered on the chat pane."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")

    title = page.get_by_test_id("header-title")
    expect(title).to_be_visible(timeout=30_000)
    conversations = page.locator(_CONVERSATIONS)
    expect(conversations).not_to_have_attribute("data-collapsed", "true")
    workspace = page.get_by_role("complementary", name="Workspace")
    expect(workspace).to_be_visible(timeout=30_000)

    # Step 2 of the journey: drag the sidebar's right-edge handle to widen it.
    handle = page.get_by_role("separator", name="Resize sidebar")
    hbox = _box(handle)
    grab_y = hbox["y"] + hbox["height"] / 2
    page.mouse.move(hbox["x"] + hbox["width"] / 2, grab_y)
    page.mouse.down()
    page.mouse.move(_SIDEBAR_TARGET_PX, grab_y, steps=20)
    page.mouse.up()
    # Let the layout settle (and give the recording a beat on the end state).
    page.wait_for_timeout(1_000)

    sidebar = _box(conversations)
    assert sidebar["width"] >= _SIDEBAR_TARGET_PX - 20, (
        f"sidebar drag did not take effect: width {sidebar['width']}px"
    )

    # The chat pane: from the sidebar's right edge to the rail's left edge.
    rail = _box(workspace)
    pane_left = sidebar["x"] + sidebar["width"]
    pane_right = rail["x"]
    pane_center = (pane_left + pane_right) / 2

    tbox = _box(title)
    title_center = tbox["x"] + tbox["width"] / 2

    # The sidebar must never sit on top of the title.
    assert tbox["x"] >= pane_left - 1, (
        f"sidebar overlaps the title: title starts at {tbox['x']:.0f}px, "
        f"sidebar's right edge is {pane_left:.0f}px"
    )

    # The reported failure: the title stays glued to the sidebar's edge
    # instead of centering on the chat pane.
    offset = title_center - pane_center
    assert abs(offset) <= _CENTER_TOLERANCE_PX, (
        f"header title is not centered on the chat pane: title center "
        f"{title_center:.0f}px vs pane center {pane_center:.0f}px "
        f"(offset {offset:+.0f}px; pane spans "
        f"{pane_left:.0f}px..{pane_right:.0f}px, title starts "
        f"{tbox['x'] - pane_left:.0f}px from the sidebar's edge)"
    )
