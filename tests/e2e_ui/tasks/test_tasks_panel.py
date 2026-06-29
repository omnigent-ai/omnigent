"""UI e2e: the left-pane Tasks panel lists work items (#3).

Seeds a work item through the REST API, then loads ``/tasks`` and asserts the
panel renders it — covering the ``useWorkItems`` hook + ``TasksPage`` + the
``/v1/work-items`` read end to end in a real browser.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect


def test_tasks_panel_lists_a_seeded_work_item(page: Page, live_server: str) -> None:
    title = f"e2e-task-{uuid.uuid4().hex[:8]}"
    resp = httpx.post(
        f"{live_server}/v1/work-items",
        json={"title": title, "source": "manual"},
        timeout=10.0,
    )
    resp.raise_for_status()

    page.goto(f"{live_server}/tasks")
    expect(page.get_by_role("heading", name="Tasks")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("task-row").filter(has_text=title)).to_be_visible(timeout=30_000)
