"""Placement of the background-task tally relative to the composer's gray bar.

The design mocks put the running-work counts — sub-agents and background
tasks — on the RIGHT side of the gray workspace bar above the composer (the
same bar that holds the working-directory and branch selectors on its
left). The observed behavior instead renders a standalone floating
"N background task(s)" pill stacked ABOVE that bar, and leaves the bar's
right side empty.

User journey these tests encode:

1. Open a session in the web UI.
2. A background task outlives the turn (e.g. a long shell command keeps
   running in the background), so the session reports a positive
   background-task count.
3. Look above the composer: the tally should sit on the right side of the
   gray workspace bar — not float as a separate chip above it.

Like ``test_working_indicator_background_tasks``, these tests drive the
real status edge through the Sessions events route (the same path the
claude-native forwarder posts to), so they are deterministic — no live LLM
turn whose timing would make the assertions flaky.

The tally locator is deliberately tolerant of the eventual in-bar
implementation: it matches today's floating pill testid as well as any
element whose testid or accessible label names the background-task count,
so the tests keep passing however the fixed control is built, as long as
it lives inside the bar on its right side.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Locator, Page, expect

_BAR = '[data-testid="composer-workspace-controls"]'

# The visible element announcing the background-task tally, wherever it
# renders: the floating pill (role=status labelled "N background task(s)
# still running") or an in-bar count that keeps an accessible
# "background task" name / a background-task testid.
_TALLY = (
    '[data-testid="background-task-pill"], '
    '[data-testid*="background-task" i], '
    '[aria-label*="background task" i]'
)


def _publish_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    background_task_count: int | None = None,
) -> None:
    """Publish a session status through the native-harness events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param status: Session status to publish, e.g. ``"idle"``.
    :param background_task_count: Background shells still running as of
        this status edge. ``None`` omits the field.
    :returns: None.
    """
    data: dict[str, object] = {"status": status}
    if background_task_count is not None:
        data["background_task_count"] = background_task_count
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def _open_session_with_background_task(
    page: Page,
    base_url: str,
    session_id: str,
) -> Locator:
    """Seed one running background task, then open the session page.

    Publishing before the navigation lands the count in the session
    snapshot, so the page hydrates it deterministically (no SSE race).

    :param page: Playwright page fixture.
    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id to open.
    :returns: The visible background-task tally locator.
    """
    _publish_status(base_url, session_id, "idle", background_task_count=1)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(_BAR)).to_be_visible(timeout=15_000)
    tally = page.locator(_TALLY).first
    expect(tally).to_be_visible(timeout=15_000)
    # Let the tally's mount animation settle so bounding boxes are stable,
    # and dwell long enough that a recording shows the state clearly.
    page.wait_for_timeout(1_500)
    return tally


def test_background_task_count_renders_in_workspace_bar_right_slot(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The background-task tally sits on the right side of the gray bar.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local
        server fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    tally = _open_session_with_background_task(page, base_url, session_id)

    bar_box = page.locator(_BAR).bounding_box()
    tally_box = tally.bounding_box()
    assert bar_box is not None, "workspace bar has no bounding box"
    assert tally_box is not None, "background-task tally has no bounding box"

    # The tally belongs INSIDE the gray bar: its vertical center must fall
    # within the bar's band, not above it.
    tally_center_y = tally_box["y"] + tally_box["height"] / 2
    assert bar_box["y"] <= tally_center_y <= bar_box["y"] + bar_box["height"], (
        "background-task tally renders outside the workspace bar "
        f"(tally box {tally_box}, bar box {bar_box}); expected the count "
        "to sit inside the gray bar above the composer"
    )

    # ...and on the bar's right side (the directory/branch selectors own
    # the left).
    tally_center_x = tally_box["x"] + tally_box["width"] / 2
    assert tally_center_x >= bar_box["x"] + bar_box["width"] / 2, (
        "background-task tally is not on the right side of the workspace "
        f"bar (tally box {tally_box}, bar box {bar_box})"
    )


def test_tally_does_not_float_above_workspace_bar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """No standalone chip floats above the gray bar while tasks run.

    A tally whose whole box sits above the bar's top edge is the
    floating-pill layout; the count belongs inside the bar itself.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local
        server fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    tally = _open_session_with_background_task(page, base_url, session_id)

    bar_box = page.locator(_BAR).bounding_box()
    tally_box = tally.bounding_box()
    assert bar_box is not None, "workspace bar has no bounding box"
    assert tally_box is not None, "background-task tally has no bounding box"

    tally_bottom = tally_box["y"] + tally_box["height"]
    assert tally_bottom > bar_box["y"], (
        "background-task tally floats as a separate chip above the "
        f"workspace bar (tally box {tally_box}, bar box {bar_box})"
    )
