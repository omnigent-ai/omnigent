"""E2E: hovering a Working-folder row must not resize or jolt the Files rail."""

from __future__ import annotations

import re
from collections.abc import Iterator
from itertools import pairwise

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

# Enough rows that the tree overflows the rail at the default viewport.
_FILE_COUNT = 120
_FILE_PREFIX = "hover_file_"
# A row that sits near the bottom once the list is scrolled all the way down.
_HOVER_FILE = f"{_FILE_PREFIX}{_FILE_COUNT - 3:03d}.txt"
_WHEEL_STEPS = 30
_WHEEL_DELTA = 40

_SCROLL_METRICS_JS = """el => ({
  scrollTop: el.scrollTop,
  scrollHeight: el.scrollHeight,
  scrollWidth: el.scrollWidth,
  clientHeight: el.clientHeight,
  clientWidth: el.clientWidth,
})"""


def _shell(base_url: str, session_id: str, command: str) -> dict:
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/shell",
        json={"command": command, "timeout": 60},
        timeout=90.0,
    )
    resp.raise_for_status()
    return resp.json()


@pytest.fixture
def overflowing_tree_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    result = _shell(
        base_url,
        session_id,
        f'for i in $(seq -w 0 {_FILE_COUNT - 1}); do echo "$i" > {_FILE_PREFIX}$i.txt; done',
    )
    assert result["exit_code"] == 0, result
    try:
        yield (base_url, session_id)
    finally:
        _shell(base_url, session_id, f"rm -f {_FILE_PREFIX}*.txt")


def _open_tree_scrolled_to_bottom(
    page: Page, base_url: str, session_id: str
) -> tuple[Locator, Locator]:
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    any_row = rail.get_by_role("button", name=re.compile(rf"^{_FILE_PREFIX}\d+\.txt$"))
    expect(any_row.first).to_be_visible(timeout=30_000)

    # Park the pointer over the chat column so no row is hovered at rest.
    page.mouse.move(200, 300)
    section = rail.locator("section")
    section.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    bottom_row = rail.get_by_role("button", name=_HOVER_FILE, exact=True)
    expect(bottom_row).to_be_visible(timeout=15_000)
    return section, bottom_row


def _intersects_viewport(page: Page, locator: Locator) -> bool:
    box = locator.bounding_box()
    viewport = page.viewport_size
    assert box is not None and viewport is not None
    return (
        box["x"] < viewport["width"]
        and box["x"] + box["width"] > 0
        and box["y"] < viewport["height"]
        and box["y"] + box["height"] > 0
    )


def test_hovering_bottom_row_keeps_scroll_area_and_shows_tooltip(
    page: Page,
    overflowing_tree_session: tuple[str, str],
) -> None:
    """Resting the pointer on a bottom row must not grow the list or hide its tooltip."""
    base_url, session_id = overflowing_tree_session
    section, bottom_row = _open_tree_scrolled_to_bottom(page, base_url, session_id)
    rest = section.evaluate(_SCROLL_METRICS_JS)
    assert rest["scrollHeight"] > rest["clientHeight"], rest

    bottom_row.get_by_text(_HOVER_FILE, exact=True).hover()
    tooltip = page.locator('div[style*="position: fixed"]', has_text=_HOVER_FILE)
    expect(tooltip).to_have_count(1)
    hovered = section.evaluate(_SCROLL_METRICS_JS)
    tooltip_box = tooltip.bounding_box()

    problems = []
    if hovered["scrollHeight"] != rest["scrollHeight"]:
        problems.append(f"scrollHeight grew {rest['scrollHeight']} -> {hovered['scrollHeight']}")
    if hovered["scrollWidth"] > hovered["clientWidth"]:
        problems.append(
            f"horizontal scrollbar: scrollWidth {hovered['scrollWidth']} "
            f"> clientWidth {hovered['clientWidth']}"
        )
    if not _intersects_viewport(page, tooltip):
        problems.append(f"tooltip rendered off screen at {tooltip_box}")
    assert not problems, problems


def test_wheel_scroll_while_hovering_bottom_row_keeps_list_still(
    page: Page,
    overflowing_tree_session: tuple[str, str],
) -> None:
    """Scrolling down past the end while hovering must not make the list jump."""
    base_url, session_id = overflowing_tree_session
    section, bottom_row = _open_tree_scrolled_to_bottom(page, base_url, session_id)

    label_box = bottom_row.get_by_text(_HOVER_FILE, exact=True).bounding_box()
    assert label_box is not None
    x = label_box["x"] + label_box["width"] / 2
    y = label_box["y"] + label_box["height"] / 2
    page.mouse.move(x, y)
    section.evaluate(
        "el => { window.__scrollTops = [el.scrollTop];"
        " el.addEventListener('scroll', () => window.__scrollTops.push(el.scrollTop)); }"
    )

    for step in range(_WHEEL_STEPS):
        page.mouse.wheel(0, _WHEEL_DELTA)
        page.wait_for_timeout(60)
        # A hand resting on a trackpad drifts by a pixel between ticks.
        page.mouse.move(x + (step % 2), y)
        page.wait_for_timeout(60)

    scroll_tops: list[float] = page.evaluate("() => window.__scrollTops")
    snap_backs = sum(1 for a, b in pairwise(scroll_tops) if b < a)
    # Only downward wheel input was sent from the very bottom, so the list must never move back up.
    assert snap_backs == 0, (
        f"list jumped while hovering at the bottom: {snap_backs} snap-backs, "
        f"scrollTop trace {scroll_tops}"
    )
