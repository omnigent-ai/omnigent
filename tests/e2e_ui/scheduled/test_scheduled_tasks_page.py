"""UI journey: the Scheduled Tasks page (``/tasks``).

Covers the Scheduled Tasks page row behavior: a task row renders the
human-readable schedule SUMMARY derived client-side from the stored RRULE
(``describeSchedule``), plus a Run-now action and a relative next-run label
("Next run in Xh"). That label is a DELTA formatted from the server's
authoritative ``next_run_at`` (nextRunAt − now) — the client never recomputes
WHICH instant is next (which couldn't match the server anchor for INTERVAL>1
rules), so the old "no client-recomputed countdown" rule still holds. See
``test_scheduled_task_row_run_controls``.

Tasks are seeded through the same REST API the page consumes
(``POST /v1/scheduled-tasks``), so this asserts the real render path
end-to-end. It's LLM-free and fast: no agent turn is dispatched — the
rows are pure UI state derived from the stored rule, so the mock LLM is
never exercised.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect


def _builtin_agent_id(base_url: str, name: str) -> str:
    """Look up a built-in agent's id by name via ``GET /v1/agents``.

    A scheduled task requires a concrete ``agent_id``; the spawned server
    pre-registers ``hello_world`` via ``--agent``, so we resolve its id to
    seed tasks against it. (No agent turn ever fires — the id only has to
    reference a real agent so the create request validates.)

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param name: Built-in agent name, e.g. ``"hello_world"``.
    :returns: The agent id.
    """
    resp = httpx.get(f"{base_url}/v1/agents", timeout=10.0)
    resp.raise_for_status()
    agents = resp.json()["data"]
    matches = [a["id"] for a in agents if a["name"] == name]
    assert matches, (
        f"built-in agent {name!r} not listed in /v1/agents "
        f"(got {[a['name'] for a in agents]}) — nothing to seed a task against."
    )
    return matches[0]


def _create_task(
    base_url: str,
    agent_id: str,
    name: str,
    rrule: str,
    *,
    host_id: str | None = None,
    workspace: str | None = None,
) -> str:
    """Seed one scheduled task via ``POST /v1/scheduled-tasks``.

    :returns: The created task id.
    """
    body = {
        "name": name,
        "prompt": "Do the thing.",
        "rrule": rrule,
        "agent_id": agent_id,
        "timezone": "UTC",
    }
    if host_id is not None:
        body["host_id"] = host_id
    if workspace is not None:
        body["workspace"] = workspace
    resp = httpx.post(
        f"{base_url}/v1/scheduled-tasks",
        json=body,
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _row_by_name(page: Page, name: str):
    """The scheduled-task row whose title matches ``name``."""
    return page.locator('[data-testid="scheduled-task-row"]').filter(has_text=name)


def test_scheduled_task_rows_show_schedule_summary_and_relative_next_run(
    page: Page,
    live_server: str,
) -> None:
    """Rows render the RRULE-derived schedule summary + the relative next-run label.

    Seeds three tasks whose summaries exercise the daily, weekdays, and
    hourly-with-minute (the ``describeSchedule`` hourly fix) cases, then asserts
    each row's schedule line shows the expected summary and that the armed rows
    carry the server-derived relative "Next run in …" label (a delta from the
    server's next_run_at, NOT a client-recomputed instant).
    """
    agent_id = _builtin_agent_id(live_server, "hello_world")

    # Daily at 9:00 AM → "Every day at 9:00 AM".
    _create_task(live_server, agent_id, "Daily digest", "FREQ=DAILY;BYHOUR=9;BYMINUTE=0")
    # Mon–Fri at 8:00 AM → "Weekdays at 8:00 AM".
    _create_task(
        live_server,
        agent_id,
        "Weekday triage",
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0",
    )
    # Hourly at :30 → "Hourly at :30" (the interval-1 non-zero-BYMINUTE fix).
    _create_task(live_server, agent_id, "Half-past sweep", "FREQ=HOURLY;BYMINUTE=30")

    page.goto(f"{live_server}/tasks")

    # All three rows render (the list query resolves against the seeded tasks).
    rows = page.locator('[data-testid="scheduled-task-row"]')
    expect(rows).to_have_count(3, timeout=30_000)

    # Each row's schedule line shows the client-derived summary text. The line
    # also carries the relative next-run suffix ("· Next run in …") for an armed
    # active task, so assert the summary is CONTAINED rather than the exact line.
    daily = _row_by_name(page, "Daily digest")
    expect(daily.get_by_test_id("task-schedule-line")).to_contain_text(
        "Every day at 9:00 AM", timeout=30_000
    )

    weekday = _row_by_name(page, "Weekday triage")
    expect(weekday.get_by_test_id("task-schedule-line")).to_contain_text("Weekdays at 8:00 AM")

    # The hourly-minute fix: interval-1 hourly with a non-zero BYMINUTE shows
    # the minute rather than a bare "Hourly".
    hourly = _row_by_name(page, "Half-past sweep")
    expect(hourly.get_by_test_id("task-schedule-line")).to_contain_text("Hourly at :30")

    # The next-run label IS shown now — but as a DELTA from the server's
    # next_run_at ("Next run in Xh/Xd"), not a client-recomputed instant. An
    # armed daily task is always <24h out, so its label is "Next run in …".
    # (The forbidden thing was a client recompute of WHICH instant is next; a
    # delta off the server value is fine.)
    expect(daily.get_by_test_id("task-next-run")).to_contain_text("Next run in", timeout=30_000)


def test_scheduled_task_create_edit_modal_and_time_picker(
    page: Page,
    live_server: str,
) -> None:
    """Create/edit modal supports typed time input and the compact minute picker.

    This stays LLM-free: creating and editing scheduled tasks only exercise
    REST + client state, and no scheduled run fires.
    """
    agent_id = _builtin_agent_id(live_server, "hello_world")

    page.goto(f"{live_server}/tasks")

    page.get_by_test_id("new-task-button").click()
    expect(page.get_by_test_id("create-scheduled-task-dialog")).to_be_visible(timeout=30_000)
    page.get_by_test_id("task-name-input").fill("Typed time daily")
    page.get_by_test_id("task-prompt-input").fill("Summarize the day.")
    agent_trigger = page.get_by_test_id("task-agent-picker").get_by_test_id(
        "new-chat-landing-agent-select"
    )
    expect(agent_trigger).to_contain_text("Claude Code", timeout=30_000)
    agent_trigger.click()
    page.get_by_role("menuitem").filter(has_text="Claude Code").click()
    expect(page.get_by_test_id("schedule-preset-trigger")).to_contain_text("Daily")

    time_input = page.get_by_test_id("schedule-time")
    time_input.fill("")
    time_input.click()
    page.keyboard.type("9:37")
    assert time_input.input_value() == "9:37"
    page.get_by_test_id("task-name-input").click()
    expect(time_input).to_have_value("09:37 AM")
    page.get_by_test_id("schedule-time-picker-trigger").click()
    minute_column = page.get_by_test_id("schedule-minute-column")
    expect(minute_column.locator('[data-testid^="schedule-minute-"]')).to_have_count(
        60,
        timeout=30_000,
    )
    expect(page.get_by_test_id("schedule-minute-37")).to_be_visible()
    page.get_by_test_id("schedule-minute-37").click(force=True)
    expect(time_input).to_have_value("09:37 AM")
    page.get_by_test_id("create-scheduled-task-submit").click()

    created_row = _row_by_name(page, "Typed time daily")
    expect(created_row).to_be_visible(timeout=30_000)
    # `to_contain_text`: the line may also carry the server next-run suffix.
    expect(created_row.get_by_test_id("task-schedule-line")).to_contain_text(
        "Every day at 9:37 AM",
        timeout=30_000,
    )

    _create_task(
        live_server,
        agent_id,
        "Edit footer task",
        "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    )
    page.set_viewport_size({"width": 900, "height": 520})
    page.reload()

    edit_row = _row_by_name(page, "Edit footer task")
    expect(edit_row).to_be_visible(timeout=30_000)
    edit_row.hover()
    edit_row.get_by_test_id("task-row-menu").click()
    page.get_by_test_id("task-edit").click()

    dialog = page.get_by_test_id("create-scheduled-task-dialog")
    submit = page.get_by_test_id("create-scheduled-task-submit")
    expect(dialog).to_be_visible(timeout=30_000)
    expect(page.get_by_role("button", name="Cancel")).to_be_visible()
    expect(submit).to_be_visible()
    dialog_box = dialog.bounding_box()
    submit_box = submit.bounding_box()
    assert dialog_box is not None
    assert submit_box is not None
    assert submit_box["y"] + submit_box["height"] <= dialog_box["y"] + dialog_box["height"] + 1

    time_input.fill("")
    time_input.click()
    page.keyboard.type("10:37")
    page.get_by_test_id("task-name-input").click()
    expect(time_input).to_have_value("10:37 AM")
    submit.click()

    expect(edit_row.get_by_test_id("task-schedule-line")).to_contain_text(
        "Every day at 10:37 AM",
        timeout=30_000,
    )


def test_scheduled_task_row_run_controls(
    page: Page,
    live_server: str,
) -> None:
    """Run controls: relative next-run label, and Run now → a run is recorded.

    LLM-free. The seeded task pins no host, so a manual Run now resolves no online
    host and records a ``failed`` run. This exercises the real
    POST /v1/scheduled-tasks/{id}/run path (the ⋯-menu action) end-to-end and
    confirms the run row is written, without ever dispatching an agent turn. The
    completion pill was removed from the row UI, so completion is asserted via the
    server-side run record rather than a visible badge.
    """
    agent_id = _builtin_agent_id(live_server, "hello_world")
    task_id = _create_task(
        live_server, agent_id, "Run-now target", "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"
    )

    page.goto(f"{live_server}/tasks")
    row = _row_by_name(page, "Run-now target")
    expect(row).to_be_visible(timeout=30_000)

    # Relative next-run renders for an armed active task — a delta off the
    # server's next_run_at ("Next run in …"), not a client-recomputed instant.
    expect(row.get_by_test_id("task-next-run")).to_contain_text("Next run in", timeout=30_000)

    # No run has fired yet → the run history is empty.
    empty = httpx.get(f"{live_server}/v1/scheduled-tasks/{task_id}/runs", timeout=10.0)
    assert empty.json()["runs"] == []

    # Trigger Run now from the ⋯ menu.
    row.hover()
    row.get_by_test_id("task-row-menu").click()
    page.get_by_test_id("task-run-now").click()

    # The manual fire runs through the shared fire path and records a run row.
    # With no online host it resolves to a ``failed`` run. Poll the history until
    # the background fire lands (the POST returns 202 before the run is written).
    def _has_failed_run() -> bool:
        runs = httpx.get(f"{live_server}/v1/scheduled-tasks/{task_id}/runs", timeout=10.0).json()[
            "runs"
        ]
        return len(runs) == 1 and runs[0]["status"] == "failed"

    deadline = 0
    while not _has_failed_run() and deadline < 100:
        page.wait_for_timeout(200)
        deadline += 1
    assert _has_failed_run(), "Run now did not record a failed run within the timeout"
