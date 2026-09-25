"""Full-stack coverage for background-task state across a reload."""

from __future__ import annotations

import httpx
from playwright.sync_api import Locator, Page, expect

_PILL = '[data-testid="background-task-pill"]'
_MONITOR_TASK = {
    "id": "monitor-ci",
    "type": "shell",
    "status": "running",
    "description": "Watch PR checks and review comments",
    "command": "gh pr checks 123 --watch",
}


def _pill_badge(page: Page, count: int) -> Locator:
    plural = "" if count == 1 else "s"
    return page.get_by_role(
        "button", name=f"{count} background task{plural} still running", exact=True
    )


def _publish_status(
    base_url: str,
    session_id: str,
    status: str = "idle",
    *,
    response_id: str | None = None,
    background_task_count: int | None = None,
    background_tasks: list[dict[str, str]] | None = None,
) -> None:
    """Publish a status edge; an omitted count preserves the sticky tally."""
    data: dict[str, object] = {"status": status}
    if response_id is not None:
        data["response_id"] = response_id
    if background_task_count is not None:
        data["background_task_count"] = background_task_count
    if background_tasks is not None:
        data["background_tasks"] = background_tasks
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    response.raise_for_status()


def test_badge_survives_reload_and_tracks_updates_after_reconnect(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Snapshot hydration and the reconnected stream keep the tally current."""
    base_url, session_id = seeded_session
    _publish_status(
        base_url,
        session_id,
        background_task_count=1,
        background_tasks=[_MONITOR_TASK],
    )
    page.goto(f"{base_url}/c/{session_id}")
    expect(_pill_badge(page, 1)).to_have_text("1", timeout=15_000)
    _pill_badge(page, 1).click()
    expect(page.get_by_role("dialog", name="1 background task", exact=True)).to_be_visible()

    page.reload()
    expect(_pill_badge(page, 1)).to_have_attribute("aria-expanded", "false", timeout=15_000)
    _pill_badge(page, 1).click()
    panel = page.get_by_role("dialog", name="1 background task", exact=True)
    expect(panel.get_by_text(_MONITOR_TASK["description"], exact=True)).to_be_visible()
    page.keyboard.press("Escape")

    _publish_status(base_url, session_id, background_task_count=2)
    expect(_pill_badge(page, 2)).to_have_text("2", timeout=15_000)
    _pill_badge(page, 2).click()
    panel = page.get_by_role("dialog", name="2 background tasks", exact=True)
    expect(panel).to_contain_text("details unavailable")
    expect(panel.get_by_role("listitem")).to_have_count(0)

    _publish_status(base_url, session_id, background_task_count=0)
    expect(page.locator(_PILL)).to_have_count(0, timeout=15_000)
    expect(panel).to_have_count(0)
