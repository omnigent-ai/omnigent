"""E2E: a two-finger pinch zooms the image lightbox on a phone in the iOS shell."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import CDPSession, Page, expect

from tests.e2e_ui.chat.test_image_lightbox_dismiss import _PROMPT, _seed_image_turn
from tests.e2e_ui.mobile.test_ios_ipad_safe_layout import _IOS_SHELL_INIT_SCRIPT

_PHONE = pytest.mark.browser_context_args(
    viewport={"width": 390, "height": 664},
    device_scale_factor=3,
    is_mobile=True,
    has_touch=True,
)

# The iOS shell turns off page zoom, so only the app can answer a pinch.
_IOS_SHELL_VIEWPORT_SCRIPT = """
document.addEventListener("DOMContentLoaded", () => {
  document.querySelector('meta[name="viewport"]').setAttribute(
    "content",
    "width=device-width, initial-scale=1.0, minimum-scale=1.0, maximum-scale=1.0, "
      + "user-scalable=no, viewport-fit=cover",
  );
}, { once: true });
"""


def _pinch(cdp: CDPSession, center: tuple[float, float], start_gap: float, end_gap: float) -> None:
    """Move two fingers along a horizontal line from ``start_gap`` to ``end_gap`` apart."""
    center_x, center_y = center

    def fingers(gap: float) -> list[dict[str, float]]:
        return [
            {"x": center_x - gap / 2, "y": center_y, "id": 0},
            {"x": center_x + gap / 2, "y": center_y, "id": 1},
        ]

    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": fingers(start_gap)})
    steps = 10
    for step in range(1, steps + 1):
        gap = start_gap + (end_gap - start_gap) * step / steps
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": fingers(gap)})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def _zoom_percent(page: Page) -> int:
    label = page.get_by_role("button", name="Reset zoom").inner_text()
    match = re.fullmatch(r"(\d+)%", label.strip())
    assert match, label
    return int(match.group(1))


def _open_image_turn_in_ios_shell(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> tuple[Page, CDPSession]:
    base_url, session_id = seeded_session
    _seed_image_turn(session_id)
    page: Page = request.getfixturevalue("page")
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    page.add_init_script(_IOS_SHELL_VIEWPORT_SCRIPT)
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")
    expect(page.get_by_text(_PROMPT)).to_be_visible(timeout=30_000)
    return page, cdp


@_PHONE
def test_pinch_zooms_the_lightbox_image_in_the_ios_shell(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """Spreading two fingers zooms the preview in; pinching them together zooms back out."""
    page, cdp = _open_image_turn_in_ios_shell(request, seeded_session)

    page.get_by_role("button", name="Zoom image: Attached image").tap()
    preview = page.get_by_role("dialog").get_by_role("img", name="Attached image")
    expect(preview).to_be_visible(timeout=10_000)
    fit_box = preview.bounding_box()
    assert fit_box and fit_box["width"] > 0
    expect(page.get_by_role("button", name="Reset zoom")).to_have_text("100%")

    center = (fit_box["x"] + fit_box["width"] / 2, fit_box["y"] + fit_box["height"] / 2)
    _pinch(cdp, center, start_gap=60, end_gap=260)
    expect(page.get_by_role("button", name="Reset zoom")).not_to_have_text("100%")
    spread_percent = _zoom_percent(page)
    spread_box = preview.bounding_box()
    assert spread_box and spread_box["width"] > fit_box["width"] * 1.2, (fit_box, spread_box)

    _pinch(cdp, center, start_gap=260, end_gap=60)
    page.wait_for_function(
        """limit => parseInt(
            document.querySelector('[aria-label="Reset zoom"]').textContent) < limit""",
        arg=spread_percent,
        timeout=5_000,
    )


@_PHONE
def test_pinch_on_an_inline_chat_image_zooms_it_in_the_ios_shell(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """A spread pinch on the transcript thumbnail opens the preview and keeps zooming it."""
    page, cdp = _open_image_turn_in_ios_shell(request, seeded_session)
    thumbnail = page.get_by_role("button", name="Zoom image: Attached image").get_by_role("img")
    thumbnail.scroll_into_view_if_needed()
    fit_box = thumbnail.bounding_box()
    assert fit_box and fit_box["width"] > 0

    center = (fit_box["x"] + fit_box["width"] / 2, fit_box["y"] + fit_box["height"] / 2)
    _pinch(cdp, center, start_gap=30, end_gap=200)
    preview = page.get_by_role("dialog").get_by_role("img", name="Attached image")
    expect(preview).to_be_visible()
    page.wait_for_function(
        """() => parseInt(
            document.querySelector('[aria-label="Reset zoom"]').textContent) > 100""",
        timeout=5_000,
    )
    page.evaluate(
        "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"
    )

    # The fingers started over the thumbnail, outside the preview image, which must stay in view.
    zoomed_box = preview.bounding_box()
    viewport = page.viewport_size
    assert zoomed_box and viewport
    middle_x, middle_y = viewport["width"] / 2, viewport["height"] / 2
    assert zoomed_box["x"] < middle_x < zoomed_box["x"] + zoomed_box["width"], zoomed_box
    assert zoomed_box["y"] < middle_y < zoomed_box["y"] + zoomed_box["height"], zoomed_box
