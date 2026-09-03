"""E2E: touch input across the web shell.

The user-facing claims, each driven here with real (CDP-synthesized)
touch input against the live SPA:

1. **Pane dividers are mouse-only.** Every resize hook binds ``onMouseDown``
   only, so a finger/stylus drag on a divider never resizes the pane. Driven
   on the left Conversations sidebar's right-edge handle: a touch drag that a
   mouse drag of the same geometry performs must change the sidebar width.

2. **Session rows expose no swipe affordance.** A deliberate horizontal swipe
   on a sidebar session row must produce *some* swipe response — the row
   tracking the finger (a transform appearing on the row while the gesture is
   in flight) or a committed swipe action (an ``archived`` PATCH). Today the
   gesture is dead: nothing moves and nothing fires.

3. **Long-press on a session row doesn't reliably open the row menu.** The
   row is wrapped in a Radix ``ContextMenuTrigger`` (which serves long-press
   on touch via the browser's contextmenu gesture) while dnd-kit's
   ``TouchSensor`` (250ms hold, 8px tolerance) arms a drag on the same
   pointer. A perfectly stationary hold happens to work, but a hold with a
   realistic few-px finger wobble — inside dnd-kit's own declared tolerance —
   opens nothing, which is exactly the "randomly does nothing" flakiness the
   report describes.

4. **Capability checks drift between call sites.** On a hover-incapable touch
   tablet the sidebar header's actions are visible (they carry the
   ``[@media((hover:hover)_and_(pointer:fine))]`` guard) while the session row's actions kebab is
   still purely hover-revealed — the same device gets different touch
   treatment from adjacent components.

All four drive their own touch-enabled contexts (the default ``page``
fixture has no touch support). Touch gestures are dispatched through CDP
``Input.dispatchTouchEvent`` so they run Chromium's real gesture recognizer
(pan/long-press/compatibility-mouse semantics), exactly as a finger would.
"""

from __future__ import annotations

import os
import time

from playwright.sync_api import Browser, CDPSession, Page, expect

_DESKTOP_VIEWPORT = {"width": 1280, "height": 800}
_PHONE_VIEWPORT = {"width": 390, "height": 844}


def _context_kwargs(**kwargs: object) -> dict:
    """Context options honoring ``OMNIGENT_E2E_RECORD_DIR``.

    These tests open their own sync contexts (the conftest recording hook
    only instruments the *async* Browser), so wire ``record_video_dir``
    through here to keep the journeys filmable.
    """
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        kwargs.setdefault("record_video_dir", record_dir)
    return kwargs


def _touch_point(x: float, y: float) -> dict:
    return {"x": round(x), "y": round(y), "radiusX": 2, "radiusY": 2, "force": 1, "id": 1}


def _touch_drag(
    cdp: CDPSession,
    start: tuple[float, float],
    offsets: list[tuple[float, float]],
    *,
    hold_before_move_s: float = 0.0,
    step_pause_s: float = 0.03,
) -> None:
    """Dispatch a touchStart → touchMove* → touchEnd sequence via CDP.

    Runs through Chromium's gesture recognizer, so the browser applies the
    same pan/scroll/long-press arbitration a real finger gets.
    """
    sx, sy = start
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [_touch_point(sx, sy)]},
    )
    if hold_before_move_s:
        time.sleep(hold_before_move_s)
    for dx, dy in offsets:
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [_touch_point(sx + dx, sy + dy)]},
        )
        time.sleep(step_pause_s)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def _sidebar_width(page: Page) -> float:
    box = page.locator('aside[aria-label="Conversations"]').bounding_box()
    assert box is not None
    return box["width"]


def test_sidebar_resize_handle_supports_touch_drag(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A touch drag on the sidebar's resize handle must resize the sidebar.

    The mouse-only-dividers facet: the resize hooks (`useResizableSidebar` and its four
    siblings) listen for ``mousedown``/``mousemove`` only. Chromium emits no
    compatibility mouse-move stream for a touch drag, so the same drag a mouse
    performs does nothing from a finger — the divider is effectively
    mouse-only. This drives the identical geometry a working mouse drag uses
    (grab the handle, pull 140px right) with a real touch sequence and asserts
    the sidebar widened.
    """
    base_url, session_id = seeded_session
    context = browser.new_context(**_context_kwargs(viewport=_DESKTOP_VIEWPORT, has_touch=True))
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
        _touch_drag(
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


def test_session_row_swipe_shows_affordance(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A horizontal swipe on a session row must produce a swipe response.

    The dead-swipe facet: session rows have no swipe affordances — archive /
    delete require opening a menu. A left swipe on the row should either track
    the finger (the row/link translating while the gesture is in flight) or
    commit a swipe action (an ``archived`` PATCH). The probe samples the row's
    and link's computed transforms every frame during the swipe (baselined
    first, so pre-existing static transforms don't count) and the test also
    watches for a session PATCH carrying ``archived``.
    """
    base_url, session_id = seeded_session
    context = browser.new_context(
        **_context_kwargs(viewport=_PHONE_VIEWPORT, has_touch=True, is_mobile=True)
    )
    try:
        page = context.new_page()

        archive_requests: list[str] = []

        def _on_request(request) -> None:
            if request.method == "PATCH" and f"/v1/sessions/{session_id}" in request.url:
                body = request.post_data or ""
                if "archived" in body:
                    archive_requests.append(body)

        page.on("request", _on_request)
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")

        row_link = page.locator(f'a[href="/c/{session_id}"]')
        expect(row_link).to_be_visible()

        # Baseline the row's transforms, then sample them every frame so any
        # gesture-driven translation during the swipe is caught.
        page.evaluate(
            """(sessionId) => {
                const link = document.querySelector(`a[href="/c/${sessionId}"]`);
                const row = link.closest('li') ?? link;
                const targets = [row, link];
                const baseline = targets.map((el) => getComputedStyle(el).transform);
                window.__swipeProbe = { moved: false, raf: 0 };
                const sample = () => {
                    targets.forEach((el, i) => {
                        const t = getComputedStyle(el).transform;
                        if (t !== baseline[i]) window.__swipeProbe.moved = true;
                    });
                    window.__swipeProbe.raf = requestAnimationFrame(sample);
                };
                sample();
            }""",
            session_id,
        )

        box = row_link.bounding_box()
        assert box is not None
        # Start toward the right edge of the row, swipe left across it.
        start_x = box["x"] + box["width"] * 0.85
        start_y = box["y"] + box["height"] / 2

        cdp = context.new_cdp_session(page)
        _touch_drag(
            cdp,
            (start_x, start_y),
            [(-25, 0), (-55, 0), (-90, 0), (-125, 0), (-160, 0)],
            hold_before_move_s=0.05,
        )

        page.wait_for_timeout(400)
        moved = page.evaluate(
            "() => { cancelAnimationFrame(window.__swipeProbe.raf);"
            " return window.__swipeProbe.moved; }"
        )
        assert moved or archive_requests, (
            "horizontal swipe on the session row produced no swipe response: the row "
            "never tracked the finger and no swipe action (archived PATCH) fired — "
            "session rows have no swipe affordance"
        )
    finally:
        context.close()


def test_session_row_long_press_opens_actions_menu(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A long-press with realistic finger wobble must open the row menu.

    The flaky-long-press facet: on touch the row menu's only long-press path is the
    Radix ``ContextMenuTrigger`` riding the browser's long-press-contextmenu
    gesture, while dnd-kit's ``TouchSensor`` (250ms hold, 8px tolerance) arms
    a drag on the very same pointer. A *perfectly stationary* hold happens to
    open the menu, but any real finger wobbles a few px during the hold — and
    a wobble of just 3px (far inside dnd-kit's own declared 8px tolerance and
    any platform's touch slop) kills the long-press, so on-device the menu
    opens "randomly" depending on how still the finger was. Holds with a ±3px
    wobble past both thresholds, releases, and requires the row actions (the
    shared ``ConversationMenuItems`` body — ``rename-conversation`` et al.)
    to be on screen.
    """
    base_url, session_id = seeded_session
    context = browser.new_context(
        **_context_kwargs(viewport=_PHONE_VIEWPORT, has_touch=True, is_mobile=True)
    )
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
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [_touch_point(x, y)]},
        )
        for i in range(6):
            time.sleep(0.12)
            dx = 3 if i % 2 == 0 else -3
            cdp.send(
                "Input.dispatchTouchEvent",
                {"type": "touchMove", "touchPoints": [_touch_point(x + dx, y)]},
            )
        time.sleep(0.3)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

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
    """On a touch tablet the session-row kebab must be visible without hover.

    The capability-drift facet: capability checks drift between call sites. The
    Projects header actions already fall back to always-visible on
    hover-incapable devices (``[@media((hover:hover)_and_(pointer:fine))]`` gating — the touch
    tablet fix), but the session row's kebab and pin — an adjacent call site
    in the same sidebar — are still purely hover-revealed
    (``md:opacity-0 md:group-hover:opacity-100`` with no hover-capability
    guard). On a touch tablet (>=768px viewport, coarse pointer, no hover)
    the row controls are therefore invisible while the header controls are
    visible: the same device gets different touch treatment per component.
    """
    base_url, session_id = seeded_session
    context = browser.new_context(
        **_context_kwargs(viewport={"width": 1024, "height": 768}, has_touch=True)
    )
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
