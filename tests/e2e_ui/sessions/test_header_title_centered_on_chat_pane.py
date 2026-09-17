"""Browser e2e for the chat-header title's horizontal placement.

The session title in the chat header should read as belonging to the chat
pane: centered on the pane's width, so widening the left sidebar (an
increasingly common layout when the right rail previews code/PDFs/websites)
doesn't shove the title deep into the window where it visually collides
with the sidebar's edge.

Journey (from the bug report):

1. Open the web UI and open a session (the title renders in the header).
2. Drag the sidebar's right-edge resize handle to widen the sidebar.
3. The title must stay centered on the chat pane — today it is anchored to
   the pane's left edge, so it tracks the sidebar's edge instead.

The assertion measures geometry rather than CSS classes so any future
centering implementation (flex order, absolute overlay, grid) passes as
long as the rendered result is centered.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

# How far (px) the title's horizontal midpoint may sit from the chat pane's
# midpoint and still count as "centered". Generous enough for sub-pixel
# rounding and the rename-affordance padding; far smaller than the
# left-anchored breadcrumb's offset, which is hundreds of px on a wide pane.
CENTER_TOLERANCE_PX = 40


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give the seeded session a stable title via ``PATCH /v1/sessions/{id}``.

    The seeded session starts untitled; the header only mounts the breadcrumb
    once a title (or a parent link) resolves, so the test pins one explicitly.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id to rename.
    :param title: The new title to set.
    """
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _horizontal_center(box: dict[str, float]) -> float:
    """Return the horizontal midpoint of a Playwright bounding box."""
    return box["x"] + box["width"] / 2


def test_header_title_centered_after_widening_sidebar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Widening the sidebar must not pull the header title off the pane's center.

    Drives the real resize affordance (the sidebar's right-edge drag handle)
    rather than seeding a persisted width, so the journey matches what a user
    does: grab the handle, drag right, look at the title.
    """
    base_url, session_id = seeded_session
    _set_title(base_url, session_id, "Centering check session")

    page.goto(f"{base_url}/c/{session_id}")

    title = page.get_by_test_id("header-title")
    expect(title).to_be_visible(timeout=30_000)
    expect(title).to_have_text("Centering check session")

    # Widen the sidebar by dragging its right-edge resize handle toward the
    # viewport's midpoint (the hook clamps at 50% of the viewport width).
    handle = page.get_by_role("separator", name="Resize sidebar")
    expect(handle).to_be_visible()
    handle_box = handle.bounding_box()
    assert handle_box is not None
    viewport = page.viewport_size
    assert viewport is not None
    target_x = int(viewport["width"] * 0.45)

    page.mouse.move(
        handle_box["x"] + handle_box["width"] / 2,
        handle_box["y"] + handle_box["height"] / 2,
    )
    page.mouse.down()
    page.mouse.move(target_x, handle_box["y"] + handle_box["height"] / 2, steps=12)
    page.mouse.up()

    # The sidebar must actually have widened, or the journey didn't happen.
    sidebar = page.get_by_role("complementary", name="Conversations")
    sidebar_box = sidebar.bounding_box()
    assert sidebar_box is not None
    assert sidebar_box["width"] >= target_x - 20, (
        f"sidebar drag did not take: width {sidebar_box['width']:.0f}px, expected ~{target_x}px"
    )

    # The chat header spans exactly the chat pane (it is absolutely inset
    # within the pane container), so its box gives the pane's extent. This
    # assertion depends on that positioning contract: if the header is ever
    # re-parented above the pane, measure the pane container instead.
    header = page.locator("header.chat-header")
    header_box = header.bounding_box()
    title_box = title.bounding_box()
    assert header_box is not None and title_box is not None

    pane_center = _horizontal_center(header_box)
    title_center = _horizontal_center(title_box)
    offset = title_center - pane_center

    assert abs(offset) <= CENTER_TOLERANCE_PX, (
        f"header title is not centered on the chat pane: title midpoint "
        f"{title_center:.0f}px vs pane midpoint {pane_center:.0f}px "
        f"(offset {offset:+.0f}px, tolerance {CENTER_TOLERANCE_PX}px; "
        f"pane spans x={header_box['x']:.0f}..{header_box['x'] + header_box['width']:.0f})"
    )
