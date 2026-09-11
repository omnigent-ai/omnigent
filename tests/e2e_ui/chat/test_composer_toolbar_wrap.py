"""E2E: the composer toolbar's two control clusters stay on one row.

When the chat column narrows (a dragged panel divider or a shrunken browser
window), the composer's bottom toolbar must keep its left-hand cluster
(add menu, connection badge, permission-mode pill) and right-hand cluster
(model/effort pill, mic, send) on a single flex line, shrinking or
truncating labels as needed. The regression under guard: the row wraps as a
whole and the model/mic/send cluster drops onto a second line below the
left-hand controls, growing the card a full row taller.

The session snapshot is rendered as claude-native with a wide permission
pill ("Bypass permissions") and a long catalog model label so the two
clusters are as wide as in the reported session; the wrap is then purely a
function of the toolbar's flex layout at narrow widths.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_claude_model_picker import _patch_session_as_claude_native

_FABLE_MODEL = "system.ai.claude-fable-5-1"
_FABLE_LABEL = "Fable 5.1 (1M context)"
_MODEL_OPTIONS = [
    {"id": "fable", "model": _FABLE_MODEL, "displayName": _FABLE_LABEL, "isDefault": True},
]

# Viewport widths the toolbar must survive without the right cluster falling
# below the left one. On the desktop layout the open sidebar + workspace rail
# leave the chat column ~465px wide, so the wide-pill toolbar is squeezed even
# at 1280; the narrower entries model a shrunken window. 740 is the band where
# the composer runs full-width and the clusters genuinely fit side by side.
_WIDTHS = [1280, 1000, 800, 740, 560, 480]

# Per-step settle after a viewport resize, long enough for the flex reflow
# (and slow-CI paint) to land before boxes are measured.
_RESIZE_SETTLE_MS = 700


@pytest.fixture(autouse=True)
def _finish_snapshot_routes(page: Page) -> Iterator[None]:
    """Drain snapshot response handlers before Playwright disposes the page."""
    yield
    page.unroute_all(behavior="wait")


def test_composer_toolbar_clusters_share_one_row_at_narrow_widths(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The model/mic/send cluster never drops below the left-hand controls.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to claude-native so the
        composer renders the wide permission-mode and model pills.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(
        page,
        session_id,
        model_options=_MODEL_OPTIONS,
        llm_model=_FABLE_MODEL,
        permission_mode="bypassPermissions",
    )

    page.set_viewport_size({"width": _WIDTHS[0], "height": 800})
    page.goto(f"{base_url}/c/{session_id}")

    row = page.get_by_test_id("composer-action-row")
    expect(row).to_be_visible(timeout=60_000)
    # Both wide pills must render before measuring, so this can never silently
    # pass against a minimal composer whose clusters would fit anywhere.
    expect(row.get_by_text("Bypass permissions")).to_be_visible(timeout=30_000)
    expect(row.get_by_text(_FABLE_LABEL)).to_be_visible(timeout=30_000)

    wrapped: list[str] = []
    for width in _WIDTHS:
        page.set_viewport_size({"width": width, "height": 800})
        page.wait_for_timeout(_RESIZE_SETTLE_MS)

        groups = row.locator("> div")
        assert groups.count() == 2, "composer action row should hold exactly two cluster groups"
        left_box = groups.nth(0).bounding_box()
        right_box = groups.nth(1).bounding_box()
        assert left_box is not None and right_box is not None

        # One flex line means the clusters' vertical centers coincide (the row
        # is items-center) and the right cluster starts above the left one's
        # bottom edge. A wrapped row fails both by a full row height.
        dropped = right_box["y"] >= left_box["y"] + left_box["height"] - 1
        center_offset = abs(
            (right_box["y"] + right_box["height"] / 2) - (left_box["y"] + left_box["height"] / 2)
        )
        if dropped or center_offset > 6:
            wrapped.append(
                f"viewport {width}px: left cluster y={left_box['y']:.0f} "
                f"h={left_box['height']:.0f}, right cluster y={right_box['y']:.0f} "
                f"h={right_box['height']:.0f} (dropped={dropped}, "
                f"center offset {center_offset:.1f}px)"
            )

    assert not wrapped, (
        "composer toolbar wrapped: the model/mic/send cluster fell below the "
        "left-hand controls at:\n" + "\n".join(wrapped)
    )
