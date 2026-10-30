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

from playwright.sync_api import Page, expect

from omnigent.db.utils import builtin_agent_id
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME
from tests.e2e_ui.scheduled.test_scheduled_tasks_page import (
    _create_task,
    _open_automations_page,
    _row_by_name,
)


def test_scheduled_task_detail_page_run_now_records_run_row(
    page: Page,
    automations_v2_server: str,
) -> None:
    """Open a task from the list, then Run now from its header records a run.

    The task has no host and none is connected, so the claimed run fails at the
    host stage. The run-history row shows the stage and reason the server's
    error code maps to, once the background fire lands.

    Icon / tooltip semantics (CalendarOff for skipped, amber triangle for
    failed, etc.) are covered by the RTL unit tests.
    """
    base_url = automations_v2_server
    task_id = _create_task(
        base_url,
        builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME),
        "Detail run-now test",
        "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    )

    _open_automations_page(page, base_url)
    row = _row_by_name(page, "Detail run-now test")
    expect(row).to_be_visible(timeout=30_000)
    row.get_by_test_id("task-row-open").click()
    page.wait_for_url(f"**/tasks/{task_id}", timeout=15_000)

    # The detail page loads the real task; component tests cover each field.
    expect(page.get_by_test_id("task-detail-title")).to_have_text(
        "Detail run-now test", timeout=30_000
    )
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
