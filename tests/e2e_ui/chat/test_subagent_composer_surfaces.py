"""The rounded sub-agent composer masks the pink tray beneath the workspace bar."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry


@pytest.mark.parametrize("width", [390, 1000])
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_subagent_composer_masks_overlap(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
    width: int,
    dark: bool,
) -> None:
    """Check actual corner geometry, label clearance, and pixels over the tray."""
    base_url, session_id, parent_id = seeded_session_pair

    def as_child(route: Route) -> None:
        response = fetch_with_retry(route)
        payload = response.json()
        payload.update(parent_session_id=parent_id, title="Explore universe repo structure")
        route.fulfill(response=response, json=payload)

    page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), as_child)
    page.set_viewport_size({"width": width, "height": 800})
    page.add_init_script(
        f"localStorage.setItem('web-theme', {json.dumps('dark' if dark else 'light')})"
    )
    page.goto(f"{base_url}/c/{session_id}")
    tray = page.get_by_test_id("composer-subagent-tray")
    bar = page.get_by_test_id("composer-workspace-controls")
    expect(tray).to_be_visible(timeout=30_000)
    expect(bar).to_be_visible()
    assert page.locator("html").evaluate("el => el.classList.contains('dark')") == dark

    tray_box = tray.bounding_box()
    bar_box = bar.bounding_box()
    label_box = tray.locator("span").bounding_box()
    assert tray_box is not None and bar_box is not None and label_box is not None
    assert tray_box["x"] == pytest.approx(bar_box["x"], abs=1)
    assert tray_box["width"] == pytest.approx(bar_box["width"], abs=1)
    overlap = tray_box["y"] + tray_box["height"] - bar_box["y"]
    radius = bar.evaluate("el => parseFloat(getComputedStyle(el).borderTopLeftRadius)")
    assert radius > 0, "the workspace bar must keep its rounded top"
    assert overlap >= radius, "the tray must extend behind the bar's curved corners"
    assert label_box["y"] + label_box["height"] < bar_box["y"], "the bar covers the label"

    # Sample inside the overlap, away from corners and labels. Changing only
    # the tray's paint must not change any workspace-bar pixels above it.
    clip = {
        "x": bar_box["x"] + bar_box["width"] / 2,
        "y": bar_box["y"] + 2,
        "width": 4,
        "height": min(4, overlap - 2),
    }
    original_style = tray.get_attribute("style")
    try:
        tray.evaluate("el => el.style.background = 'black'")
        over_black = page.screenshot(clip=clip, animations="disabled")
        tray.evaluate("el => el.style.background = 'white'")
        over_white = page.screenshot(clip=clip, animations="disabled")
        assert over_black == over_white, "the tray tint bleeds through the workspace bar"
    finally:
        tray.evaluate(
            """(el, style) => {
                if (style === null) el.removeAttribute('style');
                else el.setAttribute('style', style);
            }""",
            original_style,
        )
