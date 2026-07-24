"""UI journey: the Scheduled Tasks page (``/tasks``).

Covers the Scheduled Tasks page row behavior: a task row renders the
human-readable schedule SUMMARY derived client-side from the stored RRULE
(``describeSchedule``), and it does NOT render a "Next run in Xh"
countdown — that was deliberately removed because a client-computed
next-run can't be guaranteed to match the server's anchor for
INTERVAL>1 rules, so only the always-correct summary is shown.

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


def _create_task(base_url: str, agent_id: str, name: str, rrule: str) -> str:
    """Seed one scheduled task via ``POST /v1/scheduled-tasks``.

    :returns: The created task id.
    """
    resp = httpx.post(
        f"{base_url}/v1/scheduled-tasks",
        json={
            "name": name,
            "prompt": "Do the thing.",
            "rrule": rrule,
            "agent_id": agent_id,
            "timezone": "UTC",
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _row_by_name(page: Page, name: str):
    """The scheduled-task row whose title matches ``name``."""
    return page.locator('[data-testid="scheduled-task-row"]').filter(has_text=name)


def test_scheduled_task_rows_show_schedule_summary_without_countdown(
    page: Page,
    live_server: str,
) -> None:
    """Rows render the RRULE-derived schedule summary and no next-run countdown.

    Seeds three tasks whose summaries exercise the daily, weekdays, and
    hourly-with-minute (the ``describeSchedule`` hourly fix) cases, then
    asserts each row's schedule line shows the expected text and that the
    countdown ("Next run") is absent anywhere on the page.
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

    # Each row's schedule line shows the client-derived summary text.
    daily = _row_by_name(page, "Daily digest")
    expect(daily.get_by_test_id("task-schedule-line")).to_have_text(
        "Every day at 9:00 AM", timeout=30_000
    )

    weekday = _row_by_name(page, "Weekday triage")
    expect(weekday.get_by_test_id("task-schedule-line")).to_have_text("Weekdays at 8:00 AM")

    # The hourly-minute fix: interval-1 hourly with a non-zero BYMINUTE shows
    # the minute rather than a bare "Hourly".
    hourly = _row_by_name(page, "Half-past sweep")
    expect(hourly.get_by_test_id("task-schedule-line")).to_have_text("Hourly at :30")

    # The "Next run in Xh" countdown was removed — it must not appear anywhere
    # on the page (this pins the decision so it can't silently regress).
    expect(page.get_by_text("Next run", exact=False)).to_have_count(0)
