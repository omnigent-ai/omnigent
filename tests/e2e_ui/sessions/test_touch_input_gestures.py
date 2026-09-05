"""E2E: touch input across the web shell.

Five journeys, each driven with real (CDP-synthesized) touch input against the
live SPA so Chromium's gesture recognizer arbitrates them as a finger would:
a touch drag on a pane divider resizes it, a horizontal swipe on a session row
tracks the finger, a leftward swipe opens the default delete confirmation,
a long-press with realistic finger wobble opens the row menu, and a
hover-incapable md+ tablet keeps the row's controls visible without hover.
"""

from __future__ import annotations

import time
from typing import Literal

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e_ui._touch import new_touch_context, touch, touch_drag
from tests.e2e_ui.conftest import set_session_title

_DESKTOP_VIEWPORT = {"width": 1280, "height": 800}
_PHONE_VIEWPORT = {"width": 390, "height": 844}


def _sidebar_width(page: Page) -> float:
    box = page.locator('aside[aria-label="Conversations"]').bounding_box()
    assert box is not None
    return box["width"]


def test_sidebar_resize_handle_supports_touch_drag(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A touch drag on the sidebar's resize handle resizes the sidebar."""
    base_url, session_id = seeded_session
    context = new_touch_context(browser, viewport=_DESKTOP_VIEWPORT)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        sidebar = page.locator('aside[aria-label="Conversations"]')
        expect(sidebar).to_be_visible()
        handle = page.locator('[aria-label="Resize sidebar"]')
        expect(handle).to_be_attached()

        box = handle.bounding_box()
        assert box is not None, "resize handle has no box — did the desktop layout render?"
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + box["height"] / 2
        width_before = _sidebar_width(page)

        cdp = context.new_cdp_session(page)
        # Slow, deliberate horizontal pull — well past any slop threshold.
        touch_drag(
            cdp,
            (start_x, start_y),
            [(20, 0), (45, 0), (70, 0), (95, 0), (120, 0), (140, 0)],
            hold_before_move_s=0.15,
        )

        # Give the store a beat to propagate, then require a real resize.
        page.wait_for_timeout(300)
        width_after = _sidebar_width(page)
        assert width_after - width_before >= 60, (
            "touch drag on the sidebar resize handle did not resize the sidebar "
            f"(width {width_before:.0f}px -> {width_after:.0f}px); the divider is mouse-only"
        )
    finally:
        context.close()


@pytest.mark.parametrize("direction", [-1, 1], ids=["left", "right"])
@pytest.mark.parametrize("reduced_motion", ["no-preference", "reduce"])
def test_session_row_swipe_tracks_finger(
    browser: Browser,
    seeded_session: tuple[str, str],
    direction: int,
    reduced_motion: Literal["no-preference", "reduce"],
) -> None:
    """The fixed-width surface and its title follow the finger in both directions."""
    base_url, session_id = seeded_session
    set_session_title(base_url, session_id, "Swipe tracking")
    context = new_touch_context(browser, viewport=_PHONE_VIEWPORT, is_mobile=True)
    try:
        page = context.new_page()
        page.emulate_media(reduced_motion=reduced_motion)
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")

        row_link = page.locator(f'a[href="/c/{session_id}"]')
        expect(row_link).to_be_visible()
        expect(row_link.locator("xpath=ancestor::li[1]")).to_have_css("touch-action", "pan-y")
        page.wait_for_function(
            """() => Math.abs(document.querySelector('aside[aria-label="Conversations"]')
                .getBoundingClientRect().left) < 0.01"""
        )
        measure = """sessionId => {
            const link = document.querySelector(`a[href="/c/${sessionId}"]`);
            const surface = link.closest('[data-testid="conversation-swipe-surface"]');
            const range = document.createRange();
            range.selectNodeContents(link.querySelector('span.truncate'));
            const title = range.getBoundingClientRect();
            const box = surface.getBoundingClientRect();
            return {surfaceX: box.x, surfaceWidth: box.width,
                titleX: title.x, titleWidth: title.width};
        }"""
        baseline = page.evaluate(measure, session_id)

        box = row_link.bounding_box()
        assert box is not None
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + box["height"] / 2
        cdp = context.new_cdp_session(page)
        touch(cdp, "touchStart", start_x, start_y)
        try:
            for distance, travel in ((20, 20), (40, 40), (60, 60), (90, 78)):
                offset_x = direction * distance
                expected_travel = direction * travel
                touch(cdp, "touchMove", start_x + offset_x, start_y)
                page.wait_for_function(
                    """([sessionId, baselineX, travel]) => {
                        const link = document.querySelector(`a[href="/c/${sessionId}"]`);
                        const surface = link.closest('[data-testid="conversation-swipe-surface"]');
                        const displacement = surface.getBoundingClientRect().x - baselineX;
                        return Math.abs(displacement - travel) < 1;
                    }""",
                    arg=[session_id, baseline["surfaceX"], expected_travel],
                    timeout=2_000,
                )
                current = page.evaluate(measure, session_id)
                for target in ("surface", "title"):
                    assert current[f"{target}X"] - baseline[f"{target}X"] == pytest.approx(
                        expected_travel, abs=1
                    ), f"{target} did not follow the finger at {offset_x}px: {current}"
                    assert current[f"{target}Width"] == pytest.approx(
                        baseline[f"{target}Width"], abs=1
                    ), f"{target} shrank during the swipe: {current}"
            if reduced_motion == "reduce":
                glyph = page.get_by_test_id("conversation-swipe-reveal").locator("span").first
                assert glyph.evaluate("element => getComputedStyle(element).scale") == "none"
        finally:
            touch(cdp, "touchCancel")
        if reduced_motion == "reduce":
            surface = page.get_by_test_id("conversation-swipe-surface")
            duration = surface.evaluate("element => getComputedStyle(element).transitionDuration")
            assert float(duration.removesuffix("s")) <= 0.001
    finally:
        context.close()


def test_session_row_left_swipe_opens_default_delete_confirmation(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A leftward session-row swipe uses the default confirmed delete action."""
    base_url, session_id = seeded_session
    context = new_touch_context(browser, viewport=_PHONE_VIEWPORT, is_mobile=True)
    try:
        page = context.new_page()

        page.goto(f"{base_url}/c/{session_id}?sidebar=open")

        row_link = page.locator(f'a[href="/c/{session_id}"]')
        expect(row_link).to_be_visible()

        box = row_link.bounding_box()
        assert box is not None
        # Start toward the right edge of the row, swipe left across it.
        start_x = box["x"] + box["width"] * 0.85
        start_y = box["y"] + box["height"] / 2

        cdp = context.new_cdp_session(page)
        touch_drag(
            cdp,
            (start_x, start_y),
            [(-25, 0), (-55, 0), (-90, 0), (-125, 0), (-160, 0)],
            hold_before_move_s=0.05,
        )

        expect(page.get_by_text("Delete conversation?", exact=True)).to_be_visible()
    finally:
        context.close()


def test_session_row_long_press_opens_actions_menu(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A long-press with ±3px finger wobble still opens the row menu."""
    base_url, session_id = seeded_session
    context = new_touch_context(browser, viewport=_PHONE_VIEWPORT, is_mobile=True)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")

        row_link = page.locator(f'a[href="/c/{session_id}"]')
        expect(row_link).to_be_visible()
        box = row_link.bounding_box()
        assert box is not None
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2

        cdp = context.new_cdp_session(page)
        # Hold past dnd-kit's 250ms drag delay and the browser's ~500ms
        # long-press threshold, wobbling ±3px like a real fingertip. 3px is
        # well inside the 8px tolerance dnd-kit itself declares for the hold,
        # so a correct gesture owner must still treat this as a long-press.
        touch(cdp, "touchStart", x, y)
        for i in range(6):
            time.sleep(0.12)
            touch(cdp, "touchMove", x + (3 if i % 2 == 0 else -3), y)
        time.sleep(0.3)
        touch(cdp, "touchEnd")

        # The session actions menu (context menu body) must be on screen.
        expect(
            page.get_by_test_id("rename-conversation"),
            "long-press (with realistic finger wobble) on the session row did not "
            "open the session actions menu",
        ).to_be_visible(timeout=2_000)
    finally:
        context.close()


def test_row_actions_reachable_on_touch_tablet(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """On a hover-incapable md+ tablet the row's kebab is visible without hover."""
    base_url, session_id = seeded_session
    context = new_touch_context(browser, viewport={"width": 1024, "height": 768})
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        # This context reports itself as hover-incapable, exactly like a
        # real touch tablet.
        assert page.evaluate("matchMedia('(hover: none)').matches")

        row_link = page.locator(f'a[href="/c/{session_id}"]')
        expect(row_link).to_be_visible()

        # The fixed call site: header-level actions are visible without hover.
        expect(page.get_by_test_id("new-project")).to_be_visible()

        # The drifted call site: the row's own actions kebab must be equally
        # reachable — visible (opacity 1) without a hover the device can
        # never produce.
        kebab = row_link.locator("xpath=ancestor::li[1]").get_by_test_id("conversation-actions")
        expect(kebab).to_be_attached()
        expect(
            kebab,
            "session-row actions kebab is hover-revealed on a device with no hover: "
            "touch-capability handling drifted between the sidebar header and the row",
        ).to_have_css("opacity", "1")
    finally:
        context.close()
