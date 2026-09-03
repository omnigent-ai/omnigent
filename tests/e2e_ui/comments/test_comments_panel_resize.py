"""E2E: resizing the CommentsPanel via its divider gutter.

The comments panel is resized by dragging the slim divider gutter that sits
between the code/diff viewer and the panel (``role=separator`` labelled
"Resize comments panel"). The interaction is pointer-event driven (touch,
pen, and mouse share the same handlers), the chosen width persists across a
reload, and the gutter's invisible hit slivers are capped so pointer input
on the viewer beside the gutter must never start a resize.

Covered here:

  1. Dragging the gutter leftward widens the panel to track the pointer
     (width = panel right edge − pointer x).
  2. The dragged width survives a full page reload (persisted preference).
  3. NEGATIVE: a press-and-drag over the viewer, left of the gutter's capped
     hit sliver, does not resize the panel.

If this goes red, the likely regression is in ``useResizableCommentsPanel``
(pointer capture / clamp / persistence) or in CommentsPanel's divider-gutter
markup (the gutter must stay a flex sibling between viewer and panel).
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_FILE_PATH = "resize_target.md"

# Anchor paragraph for the seeded comment; appears exactly once so the stored
# offsets unambiguously match the file content.
_ANCHOR_TEXT = "Resize gutter anchor paragraph."

_FILE_CONTENT = f"""\
# Comments Resize Test

{_ANCHOR_TEXT}

Closing paragraph with filler text.
"""

_COMMENT_BODY = "Comment pinning the panel open for the resize test."

# Wide desktop viewport: the FileViewer rail must have enough row space that
# a leftward drag can widen the panel without hitting the dynamic clamp
# (which reserves 240px for the viewer beside the gutter).
_DESKTOP_VIEWPORT = {"width": 1728, "height": 1080}

# Preferred leftward drag distance; shrunk at runtime if the viewer has less
# spare room than this above its 240px minimum.
_DRAG_PX = 100

# The viewer's clamp-protected minimum width (MIN_VIEWER_PX in
# useResizableCommentsPanel.ts) plus slack, so the capped drag never lands in
# clamp territory and the tracking assertion stays exact.
_VIEWER_MIN_PX = 240
_CLAMP_SLACK_PX = 20

# Pixel tolerance for width assertions (sub-pixel layout rounding).
_TOLERANCE = 3.0

# The gutter's invisible hit sliver may overhang the viewer by at most this
# many px (VIEWER_SLIVER_PX in useResizableCommentsPanel.ts). The negative
# probe presses just left of this budget, measured from the VIEWER's right
# edge — never from the gutter's own box, which a hit-region regression
# would move (dragging the probe along with it and masking the leak).
_VIEWER_SLIVER_BUDGET_PX = 10

# Safety margin between the budget line and the probe press point, absorbing
# sub-pixel layout rounding while staying close enough to catch a sliver
# that grows even a few px past its cap.
_PROBE_MARGIN_PX = 4


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def commented_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a markdown file plus one open comment via REST.

    The ``?comment={id}`` deep link then opens the file with the comments
    panel already visible — no in-browser selection dance needed.

    :param seeded_session: Base fixture providing a runner-bound
        ``(base_url, session_id)`` pair.
    :returns: ``(base_url, session_id, comment_id)``.
    """
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}"
    )
    httpx.put(
        file_url,
        json={"content": _FILE_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    ).raise_for_status()

    start = _FILE_CONTENT.find(_ANCHOR_TEXT)
    assert start != -1, "fixture bug: anchor text missing from file content"
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": _FILE_PATH,
            "body": _COMMENT_BODY,
            "start_index": start,
            "end_index": start + len(_ANCHOR_TEXT),
            "anchor_content": _ANCHOR_TEXT,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id, resp.json()["id"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_panel(page: Page, base_url: str, session_id: str, comment_id: str) -> Locator:
    """Deep-link into the file with the comments panel open; return the gutter.

    :returns: The divider-gutter separator locator, ready to drag.
    """
    page.goto(f"{base_url}/c/{session_id}?file={_FILE_PATH}&comment={comment_id}")
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible(timeout=30_000)
    expect(file_viewer).to_contain_text(_COMMENT_BODY, timeout=15_000)

    separator = file_viewer.get_by_role("separator", name="Resize comments panel")
    expect(separator).to_be_visible()
    return separator


def _panel_width(separator: Locator) -> float:
    """Measure the panel root (the gutter's next sibling) rendered width."""
    return separator.evaluate("el => el.nextElementSibling.getBoundingClientRect().width")


def _panel_right(separator: Locator) -> float:
    """Measure the panel root's right edge (fixed during a drag)."""
    return separator.evaluate("el => el.nextElementSibling.getBoundingClientRect().right")


def _viewer_width(separator: Locator) -> float:
    """Measure the code/diff viewer's rendered width (the gutter's preceding sibling)."""
    return separator.evaluate("el => el.previousElementSibling.getBoundingClientRect().width")


def _viewer_right(separator: Locator) -> float:
    """Measure the code/diff viewer's right edge (the gutter's preceding sibling).

    The negative probe anchors here: the viewer's own box marks the seam the
    layout owns, independent of how far the gutter's hit sliver actually
    reaches — so a hit-region regression cannot move the probe with it.
    """
    return separator.evaluate("el => el.previousElementSibling.getBoundingClientRect().right")


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_gutter_drag_resizes_panel_and_persists(
    page: Page,
    commented_session: tuple[str, str, str],
) -> None:
    """Drag the divider gutter, verify the width tracks, persists, and is scoped."""
    base_url, session_id, comment_id = commented_session
    page.set_viewport_size(_DESKTOP_VIEWPORT)
    separator = _open_panel(page, base_url, session_id, comment_id)

    box = separator.bounding_box()
    assert box is not None, "separator has no layout box"
    start_x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    panel_right = _panel_right(separator)
    initial_width = _panel_width(separator)

    # Keep the drag inside the un-clamped window: the hook stops widening the
    # panel once the viewer would drop below its 240px minimum, so cap the
    # distance by the viewer's spare room to keep the tracking check exact.
    spare = _viewer_width(separator) - _VIEWER_MIN_PX - _CLAMP_SLACK_PX
    drag_px = min(_DRAG_PX, spare)
    assert drag_px >= 40, (
        f"viewer too narrow ({_viewer_width(separator)}px) to exercise an "
        f"un-clamped drag at {_DESKTOP_VIEWPORT['width']}px viewport"
    )

    # 1. Drag leftward: the hook derives width from the panel's right edge and
    #    the pointer position, so the final width must track the release point.
    end_x = start_x - drag_px
    page.mouse.move(start_x, y)
    page.mouse.down()
    page.mouse.move(end_x, y, steps=8)
    page.mouse.up()

    dragged_width = _panel_width(separator)
    expected = panel_right - end_x
    assert abs(dragged_width - expected) <= _TOLERANCE, (
        f"panel width {dragged_width} does not track the pointer release at "
        f"{end_x} (expected ~{expected}, started at {initial_width})"
    )

    # 2. The released width is a persisted preference: it must survive a full
    #    reload (fresh React tree, width restored from storage).
    page.reload()
    separator = _open_panel(page, base_url, session_id, comment_id)
    reloaded_width = _panel_width(separator)
    assert abs(reloaded_width - dragged_width) <= _TOLERANCE, (
        f"panel width {reloaded_width} after reload lost the dragged width {dragged_width}"
    )

    # 3. NEGATIVE: press-and-drag over the viewer, just left of the gutter's
    #    sliver budget, must not resize — the viewer keeps its own pointer
    #    stream (text selection / scrollbar). The probe is anchored to the
    #    VIEWER's right edge plus the fixed budget, NOT to the separator's own
    #    box: a regression that widens the hit sliver would shift that box and
    #    carry a box-relative probe out of harm's way, hiding the leak.
    outside_x = _viewer_right(separator) - _VIEWER_SLIVER_BUDGET_PX - _PROBE_MARGIN_PX
    page.mouse.move(outside_x, y)
    page.mouse.down()
    page.mouse.move(outside_x - 80, y, steps=5)
    page.mouse.up()

    final_width = _panel_width(separator)
    assert abs(final_width - reloaded_width) <= _TOLERANCE, (
        f"a drag starting {_PROBE_MARGIN_PX}px outside the {_VIEWER_SLIVER_BUDGET_PX}px "
        f"viewer-side sliver budget resized the panel "
        f"({reloaded_width} -> {final_width}); the hit sliver leaked over the viewer"
    )
