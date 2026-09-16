"""Working indicators remain accurate while background-task controls are hidden.

Real status events still drive working/sidebar state. The unfinished composer
background-task entry point stays hidden across status changes and navigation.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.chat._working_labels import WORKING_LABEL_RE as _WORKING_LABEL_RE

_WORKING = '[data-testid="working-indicator"]'
_PILL = '[data-testid="background-task-pill"]'


def _expect_hidden_pill(page: Page) -> None:
    expect(page.get_by_test_id("composer-workspace-controls")).to_be_visible(timeout=15_000)
    expect(page.locator(_PILL)).to_have_count(0)


def _publish_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    background_task_count: int | None = None,
) -> None:
    """Publish a status edge; an omitted count preserves the sticky tally."""
    data: dict[str, object] = {"status": status}
    if background_task_count is not None:
        data["background_task_count"] = background_task_count
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_background_task_control_stays_hidden_through_status_changes(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)
    _publish_status(base_url, session_id, "idle", background_task_count=2)
    page.goto(f"{base_url}/c/{session_id}")
    _expect_hidden_pill(page)
    expect(working).to_have_count(0)

    _publish_status(base_url, session_id, "running")
    expect(working).to_contain_text(_WORKING_LABEL_RE, timeout=15_000)
    _expect_hidden_pill(page)

    _publish_status(base_url, session_id, "idle", background_task_count=0)
    expect(working).to_have_count(0, timeout=15_000)
    _expect_hidden_pill(page)


def test_working_shimmer_remains_visible_with_task_controls_hidden(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)
    _publish_status(base_url, session_id, "idle", background_task_count=2)
    page.goto(f"{base_url}/c/{session_id}")
    _expect_hidden_pill(page)
    expect(working).to_have_count(0)

    _publish_status(base_url, session_id, "running")
    expect(working).to_contain_text(_WORKING_LABEL_RE, timeout=15_000)
    _expect_hidden_pill(page)


def test_sidebar_spinner_ignores_background_tasks(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)
    running_badge = page.locator('[data-testid="session-state-badge"][data-state="running"]')
    _publish_status(base_url, session_id, "idle", background_task_count=1)
    page.goto(f"{base_url}/c/{session_id}")
    _expect_hidden_pill(page)
    expect(running_badge).to_have_count(0)

    _publish_status(base_url, session_id, "running")
    expect(running_badge).to_have_count(1, timeout=15_000)

    _publish_status(base_url, session_id, "idle", background_task_count=0)
    expect(working).to_have_count(0, timeout=15_000)
    expect(running_badge).to_have_count(0, timeout=15_000)


def test_background_task_control_stays_hidden_after_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    _publish_status(base_url, session_id, "idle", background_task_count=2)
    page.goto(f"{base_url}/c/{session_id}")
    _expect_hidden_pill(page)
    page.reload()
    _expect_hidden_pill(page)
    _publish_status(base_url, session_id, "idle", background_task_count=3)
    _expect_hidden_pill(page)
    _publish_status(base_url, session_id, "idle", background_task_count=0)
    _expect_hidden_pill(page)


def test_background_task_control_stays_hidden_when_switching_sessions(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    base_url, session_a, session_b = seeded_session_pair
    _publish_status(base_url, session_a, "idle", background_task_count=2)
    page.goto(f"{base_url}/c/{session_a}")
    _expect_hidden_pill(page)
    page.goto(f"{base_url}/c/{session_b}")
    _expect_hidden_pill(page)
    page.goto(f"{base_url}/c/{session_a}")
    _expect_hidden_pill(page)
