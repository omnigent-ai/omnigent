"""CDP touch helpers shared by the touch-input e2e journeys.

Gestures are dispatched through ``Input.dispatchTouchEvent`` so they run
Chromium's real gesture recognizer (pan / long-press / compatibility-mouse
semantics), exactly as a finger would.
"""

from __future__ import annotations

import os
import time
from typing import Any

from playwright.sync_api import Browser, BrowserContext, CDPSession, Locator, Page

_TOUCH_ID = 1


def touch_point(x: float, y: float) -> dict[str, Any]:
    """A single-finger CDP touch point with a realistic contact radius."""
    return {"x": round(x), "y": round(y), "radiusX": 2, "radiusY": 2, "force": 1, "id": _TOUCH_ID}


def touch(
    cdp: CDPSession, event_type: str, x: float | None = None, y: float | None = None
) -> None:
    """Dispatch one bare-point touch event; ``touchEnd``/``touchCancel`` carry none.

    Keep a sequence on one helper: a ``touch_point`` record carries touch id 1
    while a bare point defaults to id 0, and mixing the two within one gesture
    reads as a second finger.
    """
    points = [] if x is None or y is None else [{"x": x, "y": y}]
    cdp.send("Input.dispatchTouchEvent", {"type": event_type, "touchPoints": points})


def touch_drag(
    cdp: CDPSession,
    start: tuple[float, float],
    offsets: list[tuple[float, float]],
    *,
    hold_before_move_s: float = 0.0,
    step_pause_s: float = 0.03,
) -> None:
    """Dispatch a touchStart → touchMove* → touchEnd sequence from ``start``.

    Each offset is relative to ``start``; the pauses let the recognizer see a
    finger-paced gesture rather than one coalesced move.
    """
    sx, sy = start
    touch(cdp, "touchStart", sx, sy)
    if hold_before_move_s:
        time.sleep(hold_before_move_s)
    for dx, dy in offsets:
        touch(cdp, "touchMove", sx + dx, sy + dy)
        time.sleep(step_pause_s)
    touch(cdp, "touchEnd")


def touch_drag_between(page: Page, start: tuple[float, float], end: tuple[float, float]) -> None:
    """One-move drag on a fresh CDP session (resize seams need only the endpoints)."""
    cdp = page.context.new_cdp_session(page)
    try:
        touch_drag(cdp, start, [(end[0] - start[0], end[1] - start[1])], step_pause_s=0)
    finally:
        cdp.detach()


def center(locator: Locator) -> tuple[float, float]:
    """Center of a locator's bounding box, for aiming a touch at it."""
    box = locator.bounding_box()
    assert box is not None, "element has no touchable bounding box"
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def new_touch_context(browser: Browser, **kwargs: Any) -> BrowserContext:
    """A touch-enabled context that honors ``OMNIGENT_E2E_RECORD_DIR``.

    The conftest recording hook only instruments the async Browser, so sync
    contexts opened by the touch journeys wire ``record_video_dir`` through
    here to stay filmable.
    """
    kwargs.setdefault("has_touch", True)
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        kwargs.setdefault("record_video_dir", record_dir)
    return browser.new_context(**kwargs)
