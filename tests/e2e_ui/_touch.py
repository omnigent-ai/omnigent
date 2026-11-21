"""CDP touch helpers shared by the touch-input e2e journeys.

Gestures are dispatched through ``Input.dispatchTouchEvent`` so they run
Chromium's real gesture recognizer (pan / long-press / compatibility-mouse
semantics), exactly as a finger would.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from playwright.sync_api import Browser, BrowserContext, CDPSession, Locator, Page, expect

from tests._helpers.polling import poll_until

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


_SIDEBAR_LEFT_SCRIPT = """() => {
    const aside = document.querySelector('aside[aria-label="Conversations"]');
    return aside === null ? null : aside.getBoundingClientRect().left;
}"""


def _sidebar_left(page: Page) -> float | None:
    return page.evaluate(_SIDEBAR_LEFT_SCRIPT)


def _sidebar_at_left_edge(page: Page) -> bool:
    left = _sidebar_left(page)
    return left is not None and abs(left) < 0.01


def _settled_sidebar_left(page: Page, *, timeout_s: float = 10.0) -> float:
    """The sidebar's left edge once it has stopped moving.

    Sampled from here rather than from page timers or animation frames, so a
    journey's fake page clock cannot stall it.
    """
    deadline = time.monotonic() + timeout_s
    last = _sidebar_left(page)
    while True:
        time.sleep(0.25)
        now = _sidebar_left(page)
        if now is not None and last is not None and abs(now - last) < 0.01:
            return now
        assert time.monotonic() < deadline, f"the session sidebar never settled (left={now})"
        last = now


def ensure_sidebar_open(page: Page, *, timeout_s: float = 10.0) -> None:
    """Leave the session sidebar fully open before a journey measures its rows.

    ``?sidebar=open`` is a one-shot request: AppShell applies it, strips it from
    the URL, and can still leave a narrow drawer closed. Once the request has
    been consumed and the drawer has stopped moving, a closed drawer gets the
    same "Open sidebar" tap a person would give it.
    """
    expect(page).not_to_have_url(re.compile(r"[?&]sidebar="), timeout=timeout_s * 1000)
    if abs(_settled_sidebar_left(page, timeout_s=timeout_s)) >= 0.01:
        button = page.get_by_role("button", name="Open sidebar", exact=True)
        if page.evaluate("navigator.maxTouchPoints > 0"):
            button.tap()
        else:
            button.click()
    poll_until(lambda: _sidebar_at_left_edge(page), timeout_s=timeout_s, interval_s=0.05)


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
