"""UI journey: the Scheduled Task DETAIL page (``/tasks/:taskId``).

Covers the browser path the component tests can't: clicking the task card on
``/tasks`` opens ``/tasks/:taskId`` with the real task, and **Run now** from the
detail header fires ``POST /v1/scheduled-tasks/{id}/run``, records a run in the
background, and the run-history section renders it.

The page ships behind ``automations_v2``, so this module runs its own server with
that feature on: Run now then goes through the claimed-run path the page is
built for. The suite's shared server keeps the legacy scheduler.

The seeded task pins no host, so a manual Run now with no online host records
a ``failed`` run (``no_online_host``). This exercises the end-to-end render
path without needing a live host or an agent turn, exactly like
``test_scheduled_task_row_run_controls`` in the sibling test file.

**Icon / tooltip semantics are NOT re-tested here.** The CalendarOff-for-skipped
icon, the amber triangle for failed runs, the unread blue dot, and all tooltip
text are already covered by the RTL unit tests in
``web/src/pages/TaskDetailPage.test.tsx``.

This E2E test focuses on the render + navigation + run-now → run-row journey
that only the live server path can exercise.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from omnigent.db.utils import builtin_agent_id
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME
from tests.e2e_ui.scheduled.test_scheduled_tasks_page import (
    _create_task,
    _open_automations_page,
    _row_by_name,
)


@pytest.mark.parametrize("viewport_width", [1280, 390])
def test_scheduled_task_detail_page_run_now_records_run_row(
    page: Page,
    automations_v2_server: str,
    viewport_width: int,
) -> None:
    """Open a task from the list, then Run now from its header records a run.

    The task has no host and none is connected, so the claimed run fails at the
    host stage. The run-history row shows the stage and reason the server's
    error code maps to, once the background fire lands.

    Icon / tooltip semantics (CalendarOff for skipped, amber triangle for
    failed, etc.) are covered by the RTL unit tests.
    """
    page.set_viewport_size({"width": viewport_width, "height": 900})
    task_name = f"Detail run-now test {viewport_width}"
    base_url = automations_v2_server
    task_id = _create_task(
        base_url,
        builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME),
        task_name,
        "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    )

    _open_automations_page(page, base_url)
    row = _row_by_name(page, task_name)
    expect(row).to_be_visible(timeout=30_000)
    row.get_by_test_id("task-row-open").click()
    page.wait_for_url(f"**/tasks/{task_id}", timeout=15_000)

    # The detail page loads the real task; component tests cover each field.
    expect(page.get_by_test_id("task-detail-title")).to_have_text(task_name, timeout=30_000)
    expect(page.get_by_test_id("task-detail-schedule")).to_contain_text(
        "Every day at 9:00 AM", timeout=15_000
    )
    expect(page.get_by_test_id("task-detail-host")).to_contain_text("Most recently active host")
    expect(page.get_by_test_id("task-detail-runs-empty")).to_be_visible()

    # Run now returns 202; the page polls its run history until the run lands.
    page.get_by_test_id("task-detail-run-now").click()
    run_row = page.get_by_test_id("task-detail-runs").get_by_test_id("task-detail-run").first
    expect(run_row).to_have_attribute("data-run-status", "failed", timeout=30_000)
    expect(run_row.get_by_test_id("run-failure")).to_have_text(
        "Host · None of your hosts was online."
    )

    # The card warns even while paused; the sessionless failure also reaches Inbox.
    page.goto(f"{base_url}/tasks")
    row = _row_by_name(page, task_name)
    expect(row.get_by_role("img", name="Last run failed")).to_be_visible()
    row.get_by_test_id("task-row-menu").click()
    page.get_by_test_id("task-pause-toggle").click()
    expect(row.get_by_test_id("task-paused-pill")).to_be_visible()
    expect(row.get_by_role("img", name="Last run failed")).to_be_visible()

    # Real API cross-check: this failure has no session to duplicate in Inbox.
    response = page.request.get(f"{base_url}/v1/scheduled-tasks/inbox")
    assert response.ok
    [failure] = [item for item in response.json()["data"] if item["task_id"] == task_id]
    assert failure["task_id"] == task_id and failure["error_code"] == "no_online_host"
    page.keyboard.press("ControlOrMeta+k")
    page.get_by_role("dialog").get_by_text("Go to Inbox", exact=True).click()
    expect(page).to_have_url(f"{base_url}/inbox")
    entry = page.get_by_test_id("inbox-automation-item").filter(has_text=task_name)
    expect(entry).to_have_count(1)
    entry.get_by_role("button", name=f"Automation failed: {task_name}").click()
    expect(entry).to_contain_text("Host · None of your hosts was online.")
    expect(entry.get_by_role("link", name="Open automation")).to_have_attribute(
        "href", f"/tasks/{task_id}"
    )
    entry.get_by_role("link", name="Open automation").click()
    page.wait_for_url(f"**/tasks/{task_id}")
    page.goto(f"{base_url}/inbox")
    entry.get_by_role("button", name=f"Automation failed: {task_name}").click()
    entry.get_by_role("button", name="Mark as read").click()
    expect(entry).to_have_count(0)
    page.reload()
    expect(page.get_by_role("heading", name="Inbox", exact=True)).to_be_visible()
    expect(entry).to_have_count(0)
    page.keyboard.press("ControlOrMeta+k")
    page.get_by_role("dialog").get_by_text("Go to Automations", exact=True).click()
    expect(page).to_have_url(f"{base_url}/tasks")
    expect(
        _row_by_name(page, task_name).get_by_role("img", name="Last run failed")
    ).to_be_visible()
