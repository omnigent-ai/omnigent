"""Browser e2e: sidebar touch input on touch-capable wide viewports.

The sidebar's resize divider must not be mouse-only: the right-edge handle's
drag lifecycle (``useResizableSidebar``) used to listen to ``mousedown`` +
window ``mousemove``/``mouseup`` only, so a finger or stylus drag produced
pointer/touch events the handle never saw and the sidebar never resized.

The journey drives trusted touch input through CDP
``Input.dispatchTouchEvent`` so the browser produces the same
pointer/touch event stream a real touchscreen does (and mouse-only
handlers correctly receive nothing).
"""

from __future__ import annotations

import os

from playwright.sync_api import Browser, CDPSession, expect

# A touch-enabled desktop/tablet viewport: wide enough for the md+ layout
# (persistent sidebar + visible resize divider), with a coarse touch pointer
# (Playwright's has_touch flips `(pointer: coarse)` / `(hover: none)`).
_TOUCH_DESKTOP = {"width": 1280, "height": 800}


# The finger travels this far right across the divider; far past any
# reasonable drag slop and big enough that a working resize is unmissable.
_DRAG_PX = 140
# A working drag must grow the sidebar by most of the travel (clamps and
# rubber-banding get some slack).
_MIN_GROWTH_PX = 100


def _touch(cdp: CDPSession, type_: str, points: list[dict[str, float]]) -> None:
    """Dispatch one trusted touch event via CDP.

    :param cdp: CDP session for the page under test.
    :param type_: ``touchStart`` / ``touchMove`` / ``touchEnd``.
    :param points: Active touch points (empty for ``touchEnd``).
    """
    cdp.send("Input.dispatchTouchEvent", {"type": type_, "touchPoints": points})


def _context_kwargs(viewport: dict[str, int]) -> dict:
    """Touch-context kwargs, honoring the recording harness when present."""
    kwargs: dict = {"viewport": viewport, "has_touch": True}
    # The autouse _record_video fixture only patches the async API; this test
    # drives the sync API through its own context, so honor the env directly.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        kwargs["record_video_dir"] = record_dir
    return kwargs


def test_sidebar_divider_touch_drag_resizes(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A touch drag on the sidebar's resize divider must resize the sidebar.

    Journey: open a session on a touch-enabled desktop viewport -> put a
    finger on the sidebar's right-edge resize divider -> drag it 140px to
    the right -> the sidebar tracks the finger and ends up wider.

    Failure mode this catches: the divider's drag lifecycle
    is mouse-only (``onMouseDown`` + window mouse listeners), so the touch
    drag emits pointer/touch events nobody consumes and the sidebar width
    never changes.

    :param browser: Playwright browser to open the touch context on.
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    context = browser.new_context(**_context_kwargs(_TOUCH_DESKTOP))
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        sidebar = page.locator('aside[aria-label="Conversations"]')
        expect(sidebar).to_be_visible(timeout=30_000)
        # The session list hydrates before we start dragging, so the clip
        # (and the drag) run against the settled layout.
        expect(page.locator(f'a[href="/c/{session_id}"]')).to_be_visible(timeout=30_000)

        handle = page.get_by_role("separator", name="Resize sidebar")
        expect(handle).to_be_visible()

        before = sidebar.bounding_box()
        handle_box = handle.bounding_box()
        assert before is not None and handle_box is not None
        start_x = handle_box["x"] + handle_box["width"] / 2
        start_y = handle_box["y"] + handle_box["height"] / 2

        # Finger down on the divider, drag right in 10px steps, lift.
        cdp = context.new_cdp_session(page)
        _touch(cdp, "touchStart", [{"x": start_x, "y": start_y, "id": 1}])
        step = 10
        for dx in range(step, _DRAG_PX + 1, step):
            _touch(cdp, "touchMove", [{"x": start_x + dx, "y": start_y, "id": 1}])
            page.wait_for_timeout(30)
        _touch(cdp, "touchEnd", [])
        page.wait_for_timeout(500)

        after = sidebar.bounding_box()
        assert after is not None
        growth = after["width"] - before["width"]
        assert growth >= _MIN_GROWTH_PX, (
            f"touch-dragging the sidebar resize divider {_DRAG_PX}px right grew "
            f"the sidebar by only {growth:.0f}px (from {before['width']:.0f}px "
            f"to {after['width']:.0f}px); the divider is mouse-only, so touch "
            "users cannot resize the pane"
        )
    finally:
        context.close()
