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

    panel = page.get_by_test_id("agent-info-panel")
    # Hover opens without a click.
    page.get_by_test_id("agent-info-trigger").hover()
    expect(panel).to_be_visible()

    # Moving the pointer from the icon onto the panel must keep it open: the
    # leave off the icon schedules a close that entering the panel cancels.
    panel.hover()
    expect(panel).to_be_visible()
    # Give any errant close timer time to fire; the panel must still be open
    # because the pointer is resting on it.
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
    """Click opens the panel, and clicking again closes it.

    Uses ``dispatch_event("click")`` so the click fires without Playwright's
    implicit hover-move first — otherwise the hover would open the panel and the
    click would toggle it shut, hiding whether click-to-open itself works. This
    keeps click/keyboard access working independently of the hover wiring.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    _open_trigger(page)

    trigger = page.get_by_test_id("agent-info-trigger")
    panel = page.get_by_test_id("agent-info-panel")

    trigger.dispatch_event("click")
    expect(panel).to_be_visible()

    trigger.dispatch_event("click")
    expect(panel).to_be_hidden(timeout=5_000)


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
