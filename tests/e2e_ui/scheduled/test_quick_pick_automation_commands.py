"""E2E: with ``quick_pick_palette`` on, ⌘K runs the automation pages' own actions.

From an automation's page, ⌘K pauses and resumes it through the header's own
handlers, which the server records, and opens its Edit dialog. From the
Automations list, it pauses any listed automation and opens New automation. Each command's
rules and the other screens' commands are covered by Vitest.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from omnigent.db.utils import builtin_agent_id
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME
from tests._helpers.polling import poll_until
from tests.e2e_ui._quick_pick_palette import enable_quick_pick_palette, run_palette_command
from tests.e2e_ui.scheduled.test_scheduled_tasks_page import _create_task, _open_automations_page


def test_quick_pick_runs_the_automation_header_actions(
    page: Page, automations_v2_server: str
) -> None:
    """Pause, Resume and Edit from ⌘K act on the open automation."""
    base_url = automations_v2_server
    task_name = "Palette automation"
    task_id = _create_task(
        base_url,
        builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME),
        task_name,
        "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    )

    def state() -> str:
        response = page.request.get(f"{base_url}/v1/scheduled-tasks/{task_id}")
        assert response.ok
        return response.json()["state"]

    enable_quick_pick_palette(page)
    page.goto(f"{base_url}/tasks/{task_id}")
    expect(page.get_by_test_id("task-detail-title")).to_have_text(task_name, timeout=30_000)
    toggle = page.get_by_test_id("task-detail-pause-toggle")
    expect(toggle).to_be_checked()

    run_palette_command(page, "pause automation", "Pause automation", closes=True)
    expect(toggle).not_to_be_checked(timeout=10_000)
    poll_until(lambda: state() == "paused")

    run_palette_command(page, "resume automation", "Resume automation", closes=True)
    expect(toggle).to_be_checked(timeout=10_000)
    poll_until(lambda: state() == "active")

    # Edit opens the header's own dialog once the palette has closed.
    run_palette_command(page, "edit automation", "Edit automation…", closes=True)
    expect(page.get_by_role("dialog", name="Edit automation")).to_be_visible(timeout=10_000)


def test_quick_pick_runs_the_automation_list_actions(
    page: Page, automations_v2_server: str
) -> None:
    """From the list, ⌘K pauses an automation by name and opens New automation."""
    base_url = automations_v2_server
    task_name = "Palette list automation"
    task_id = _create_task(
        base_url,
        builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME),
        task_name,
        "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    )

    def state() -> str:
        response = page.request.get(f"{base_url}/v1/scheduled-tasks/{task_id}")
        assert response.ok
        return response.json()["state"]

    enable_quick_pick_palette(page)
    _open_automations_page(page, base_url)
    row = page.get_by_test_id("scheduled-task-row").filter(has_text=task_name)
    expect(row).to_be_visible(timeout=30_000)

    run_palette_command(page, f"pause {task_name}", f"Pause “{task_name}”", closes=True)
    poll_until(lambda: state() == "paused")

    # New automation opens the page's own editor once the palette has closed.
    run_palette_command(page, "new automation", "New automation…", closes=True)
    expect(page.get_by_role("dialog", name="New automation")).to_be_visible(timeout=10_000)
