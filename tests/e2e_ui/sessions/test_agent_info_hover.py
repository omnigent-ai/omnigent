"""Browser e2e for the agent-info popover's hover interaction.

The top-right agent-info (i) icon opens its panel on mouse hover and keeps it
open while the pointer rests on either the icon or the panel — a short close
delay bridges the small gap between them so the panel doesn't flicker shut when
the mouse crosses it. Click still toggles the panel, and a touch/pen tap falls
through to the native click-to-open (hover is gated to a real mouse pointer).

These behaviors live in ``web/src/components/AgentInfo.tsx``
(``AgentInfoButton``): ``onPointerEnter``/``onPointerLeave`` gated on
``pointerType === "mouse"``, a ``HOVER_CLOSE_DELAY_MS`` (150ms) close timer, and
Radix's native click/tap toggle. The component/unit suite drives these with fake
timers; this e2e proves the same flow in a real browser, which is where the
pointer-type gating and the hover-to-panel bridge actually behave differently
from synthetic events.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, Page, expect

# Comfortably longer than the component's ``HOVER_CLOSE_DELAY_MS`` (150ms) so a
# post-leave assertion observes the settled (closed) state, not the bridge
# window. Kept local to avoid coupling the test to the exact TS constant.
_CLOSE_SETTLE_MS = 600

# Dwell time while the pointer sits in the icon→panel gap. A fraction of the
# 150ms close delay: long enough that a bridge-less (immediate-close) or
# near-zero-delay implementation would have shut the panel by now, short enough
# that the real 150ms bridge still holds it open until the pointer lands on the
# panel and cancels the pending close.
_GAP_DWELL_MS = 60

# Time for the popover's open animation (``duration-150`` zoom/slide) to settle
# so ``bounding_box`` reports stable, untransformed geometry. Spent while the
# pointer still rests on the trigger, so the panel cannot close during it.
_ANIM_SETTLE_MS = 250


def _open_trigger(page: Page) -> Page:
    """Navigate to the seeded chat and wait for the info trigger to mount.

    The header info button only renders once the session binds and hydrates, so
    it can lag behind ``goto``; callers hover/click it, which flakes if done
    mid-hydration.

    :param page: Playwright page to drive.
    :returns: The same page, with the trigger visible.
    """
    trigger = page.get_by_test_id("agent-info-trigger")
    expect(trigger).to_be_visible(timeout=30_000)
    return page


# The header info trigger only mounts once the session binds and hydrates, so a
# hover/click landing mid-hydration can miss it. Rerun rather than paper over
# the race with longer per-action waits (matches test_agent_info_copy_session_id).
@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_agent_info_opens_on_hover_and_bridges_to_panel(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Hovering the (i) icon opens the panel; moving onto the panel keeps it open.

    This is the core new flow. Failure modes it catches:

    - Hover-open regressed to click-only (``onPointerEnter`` dropped or the
      ``pointerType === "mouse"`` gate rejects the real mouse pointer).
    - The close-delay bridge is gone, so crossing the gap from the icon to the
      panel fires a leave that shuts the panel before the pointer lands on it
      (the panel flickers shut and can never be reached by hover).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    _open_trigger(page)

    trigger = page.get_by_test_id("agent-info-trigger")
    panel = page.get_by_test_id("agent-info-panel")

    # Hover the icon (real mouse move) — the panel opens without a click.
    trigger.hover()
    expect(panel).to_be_visible()
    # Let the open animation settle so the boxes below are stable geometry.
    page.wait_for_timeout(_ANIM_SETTLE_MS)

    # The panel is anchored just below the icon (Radix side="bottom",
    # sideOffset=4), so there's a real vertical gap between them. Walk the
    # pointer down through that gap in small steps, pausing in the empty space
    # so a bridge-less implementation has a chance to fire its close, then land
    # on the panel. If the close-delay bridge were removed, leaving the icon
    # would schedule an immediate close and the mid-transit assertion (or the
    # landing) would find the panel already gone.
    trigger_box = trigger.bounding_box()
    panel_box = panel.bounding_box()
    assert trigger_box is not None, "trigger has no bounding box"
    assert panel_box is not None, "panel has no bounding box"

    # A vertical line at the icon's center x that also lies within the panel's
    # x-range, so every point on the descent is over either the icon, the gap,
    # or the panel — never off to the side.
    cross_x = trigger_box["x"] + trigger_box["width"] / 2
    assert panel_box["x"] <= cross_x <= panel_box["x"] + panel_box["width"], (
        "icon center x is not within the panel's x-range; the vertical transit "
        "would leave the panel's column"
    )
    gap_top = trigger_box["y"] + trigger_box["height"]
    gap_bottom = panel_box["y"]
    # The gap must be real for the transit to be meaningful; if the panel abutted
    # or overlapped the icon there'd be nothing to bridge.
    assert gap_bottom > gap_top, f"expected a gap below the icon, got {gap_top}..{gap_bottom}"

    # Step through the gap, dwelling in the empty middle.
    page.mouse.move(cross_x, gap_top + (gap_bottom - gap_top) * 0.5)
    page.wait_for_timeout(_GAP_DWELL_MS)
    # Still crossing empty space — the bridge must be holding the panel open.
    expect(panel).to_be_visible()

    # Land on the panel (just inside its top edge) and confirm it's still open,
    # then dwell well past the close delay: resting on the panel cancels any
    # pending close, so it must stay open.
    page.mouse.move(cross_x, panel_box["y"] + 5)
    expect(panel).to_be_visible()
    page.wait_for_timeout(_CLOSE_SETTLE_MS)
    expect(panel).to_be_visible()


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_agent_info_closes_after_delay_when_pointer_leaves_both(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Leaving both the icon and the panel closes the panel after the delay.

    Failure mode this catches: the leave handler never schedules the close (the
    panel stays open forever once hover-opened), or it closes immediately with
    no delay (which would defeat the icon→panel bridge).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    _open_trigger(page)

    panel = page.get_by_test_id("agent-info-panel")
    page.get_by_test_id("agent-info-trigger").hover()
    expect(panel).to_be_visible()

    # Move the pointer well off both the icon and the panel (top-left corner;
    # the panel is anchored top-right). The close timer then fires.
    page.mouse.move(5, 5)
    expect(panel).to_be_hidden(timeout=5_000)


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_agent_info_click_toggles_panel(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A real mouse click toggles the hover-opened panel shut.

    Real-input path chosen: a genuine mouse pointer (``hover`` + ``click``, not
    ``dispatch_event``), because that is the actual interaction a mouse user
    has. On a real mouse you cannot press the icon without first moving the
    pointer onto it, and that move already hover-opens the panel — so the
    meaningful thing a click does on a mouse is *toggle the open panel shut*.
    This asserts exactly that against real browser pointer/mouse events:

    1. Hover the icon (real mouse move) → the panel opens.
    2. Real ``click()`` on the icon → the panel toggles closed and stays closed
       (the pointer is still on the icon, but a click that lands closed must not
       immediately re-open — this is the "no double-open" contract).

    The click-to-open direction on a pointer where hover does *not* apply is
    covered by :func:`test_agent_info_touch_tap_opens_panel`; here the point is
    that a real click drives the toggle rather than a synthetic DOM event.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    _open_trigger(page)

    trigger = page.get_by_test_id("agent-info-trigger")
    panel = page.get_by_test_id("agent-info-panel")

    # A real mouse move onto the icon hover-opens the panel.
    trigger.hover()
    expect(panel).to_be_visible()

    # A real click then toggles it shut...
    trigger.click()
    expect(panel).to_be_hidden(timeout=5_000)
    # ...and it stays shut: the click cleared the hover-open flag, so the
    # pointer still resting on the icon must not re-open it.
    page.wait_for_timeout(_CLOSE_SETTLE_MS)
    expect(panel).to_be_hidden()


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_agent_info_touch_tap_opens_panel(
    page: Page,
    seeded_session: tuple[str, str],
    browser: Browser,
) -> None:
    """A touch tap opens the panel via the native click-to-open path.

    Hover-open is gated to ``pointerType === "mouse"``, so a tap's synthetic
    pointerenter is a no-op and the follow-up click (Radix's native toggle) is
    what opens the panel. Failure mode this catches: the gate is dropped, so the
    tap's pointerenter opens the panel only for the synthetic click to toggle it
    straight back shut — a tap could then never open it.

    Runs in a dedicated ``has_touch`` context (the default ``page`` fixture has
    no touch support, and ``tap`` requires it) at a desktop-width viewport so
    the desktop-only (``md:``) trigger still renders.

    :param page: Unused default page fixture (kept for suite-consistent
        signature); the test drives its own touch context.
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    :param browser: Playwright browser to open a touch-enabled context on.
    """
    base_url, session_id = seeded_session

    context = browser.new_context(
        has_touch=True,
        viewport={"width": 1280, "height": 720},
    )
    try:
        touch_page = context.new_page()
        touch_page.goto(f"{base_url}/c/{session_id}")
        _open_trigger(touch_page)

        touch_page.get_by_test_id("agent-info-trigger").tap()
        expect(touch_page.get_by_test_id("agent-info-panel")).to_be_visible()
    finally:
        context.close()
