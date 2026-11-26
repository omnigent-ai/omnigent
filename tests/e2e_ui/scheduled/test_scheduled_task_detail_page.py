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

An automation that continues one session gets a run per firing in that session,
and the runs endpoint repeats the session's running total on each. The second
journey checks that the page lists those runs under the session, with its total
shown once. No API writes run history or a session's recorded cost, so the test
seeds those rows with the server's stores; the sessions and the automation come
from the REST API.

**Icon / tooltip semantics are NOT re-tested here.** The CalendarOff-for-skipped
icon, the amber triangle for failed runs, the unread blue dot, and all tooltip
text are already covered by the RTL unit tests in
``web/src/pages/TaskDetailPage.test.tsx``.

These E2E tests focus on the render and navigation journeys that only the live
server path can exercise.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.db.utils import builtin_agent_id
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle
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
        model_override="opus",
        reasoning_effort="high",
        permission_mode="acceptEdits",
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
    expect(page.get_by_test_id("task-detail-runs-empty")).to_be_visible()

    # Configuration starts collapsed; it opens by click and closes by keyboard.
    configuration = page.get_by_role("button", name="Configuration", exact=True)
    expect(configuration).to_have_attribute("aria-expanded", "false")
    expect(page.get_by_test_id("task-detail-model")).not_to_be_visible()
    configuration.click()
    expect(configuration).to_have_attribute("aria-expanded", "true")
    expect(page.get_by_test_id("task-detail-host")).to_contain_text("Most recently active host")
    expect(page.get_by_test_id("task-detail-model")).to_have_text("Opus")
    expect(page.get_by_test_id("task-detail-effort")).to_have_text("High")
    expect(page.get_by_test_id("task-detail-permission")).to_have_text("Accept edits")
    expect(page.get_by_test_id("task-detail-timezone")).to_have_text("UTC")
    configuration.press("Enter")
    expect(configuration).to_have_attribute("aria-expanded", "false")
    expect(page.get_by_test_id("task-detail-model")).not_to_be_visible()
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


def _create_owned_session(base_url: str, title: str) -> tuple[str, str]:
    """Create a top-level session the local user owns; return its and its agent's ids."""
    response = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        _build_hello_world_bundle(),
        metadata={"title": title},
        timeout=30.0,
    )
    response.raise_for_status()
    session_id = response.json()["session_id"]
    session = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    session.raise_for_status()
    return session_id, session.json()["agent_id"]


# Seeds rows with this checkout's stores, whose schema an older server may not have.
@pytest.mark.min_server_version("0.24.0")
def test_scheduled_task_detail_page_lists_runs_under_their_shared_session(
    page: Page,
    automations_v2_server: str,
    automations_v2_server_dir: Path,
) -> None:
    """Runs in the continued session sit under it, which shows its total once.

    The automation once started a new session per run, so its oldest run has a
    session of its own and keeps that session's cost on its row.
    """
    base_url = automations_v2_server
    database_uri = f"sqlite:///{automations_v2_server_dir / 'chat.db'}"
    continued_title = "Factory PR triage"
    continued_id, agent_id = _create_owned_session(base_url, continued_title)
    own_id, _ = _create_owned_session(base_url, "Factory PR triage, first run")
    response = httpx.post(
        f"{base_url}/v1/scheduled-tasks",
        json={
            "name": "Shared session runs",
            "prompt": "Triage the new pull requests.",
            "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "agent_id": agent_id,
            "timezone": "UTC",
            "session_mode": "continue",
            "session_id": continued_id,
        },
        timeout=10.0,
    )
    response.raise_for_status()
    task_id = response.json()["id"]
    # Paused, so no firing can add a run while the page is checked.
    httpx.patch(
        f"{base_url}/v1/scheduled-tasks/{task_id}", json={"state": "paused"}, timeout=10.0
    ).raise_for_status()

    conversations = SqlAlchemyConversationStore(database_uri)
    conversations.set_session_usage(continued_id, {"total_cost_usd": 66.24})
    conversations.set_session_usage(own_id, {"total_cost_usd": 3.07})
    runs = SqlAlchemyScheduledTaskStore(database_uri)
    now = int(time.time())
    # Newest first: three firings into the continued session, one of which failed
    # to launch, then the run from when each run started its own session.
    for minutes_ago, status, session_id in (
        (10, "succeeded", continued_id),
        (20, "failed", continued_id),
        (30, "succeeded", continued_id),
        (2 * 24 * 60, "succeeded", own_id),
    ):
        fired_at = now - minutes_ago * 60
        failed = status == "failed"
        runs.create_run(
            uuid.uuid4().hex,
            task_id,
            status,
            fired_at,
            conversation_id=session_id,
            fired_at=fired_at,
            finished_at=fired_at + 1,
            error="runner launch/dispatch failed" if failed else None,
            error_code="launch_failed" if failed else None,
        )

    # The server repeats the session's total on each of its runs.
    listed = httpx.get(f"{base_url}/v1/scheduled-tasks/{task_id}/runs", timeout=10.0)
    listed.raise_for_status()
    assert [run["session_cost_usd"] for run in listed.json()["runs"]] == [
        66.24,
        66.24,
        66.24,
        3.07,
    ]

    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{base_url}/tasks/{task_id}")
    run_list = page.get_by_test_id("task-detail-runs")
    group = run_list.get_by_test_id("run-session-group")
    expect(group).to_have_count(1, timeout=30_000)
    header = group.get_by_test_id("run-session-group-open")
    expect(header).to_contain_text(continued_title)
    expect(group.get_by_test_id("run-session-group-cost")).to_have_text("$66.24 total")
    grouped_runs = page.get_by_role("list", name=f"Runs in {continued_title}").get_by_test_id(
        "task-detail-run"
    )
    expect(grouped_runs).to_have_count(3)
    expect(grouped_runs.nth(1)).to_have_attribute("data-run-status", "failed")
    expect(group.get_by_test_id("run-cost")).to_have_count(0)
    own_run = run_list.locator(":scope > [data-testid='task-detail-run']")
    expect(own_run).to_have_count(1)
    expect(own_run.get_by_test_id("run-cost")).to_have_text("$3.07")
    expect(own_run.get_by_test_id("run-open")).to_have_attribute("href", f"/c/{own_id}")

    header.click()
    page.wait_for_url(f"**/c/{continued_id}", timeout=15_000)
