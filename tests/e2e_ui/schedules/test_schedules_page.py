"""UI e2e: the global Schedules page lists loops (#6).

Seeds a global (agent-targeted) loop and a conversation-scoped loop via REST,
loads ``/schedules``, and asserts both rows render with their targets —
covering the useAllSchedules hook + SchedulesPage + the ``/v1/schedules`` read
in a real browser. Fire behavior (spawn / dispatch / stamp) is covered by the
runtime unit tests.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect


def test_schedules_page_lists_global_and_conversation_loops(
    page: Page, live_server: str, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    tag = uuid.uuid4().hex[:8]

    # A GLOBAL loop targets a registered agent (fresh run per fire). Hourly —
    # global loops are rate-floored to at most one fire per 5 minutes.
    httpx.post(
        f"{live_server}/v1/schedules",
        json={
            "agent_name": "hello_world",
            "name": f"e2e-global-{tag}",
            "kind": "loop",
            "prompt": "p",
            "cron": "0 * * * *",
        },
        timeout=10.0,
    ).raise_for_status()
    # A conversation-scoped loop fires into an existing session.
    httpx.post(
        f"{live_server}/v1/schedules",
        json={
            "conversation_id": session_id,
            "name": f"e2e-conv-{tag}",
            "kind": "loop",
            "prompt": "p",
            "cron": "*/10 * * * *",
        },
        timeout=10.0,
    ).raise_for_status()

    page.goto(f"{base_url}/schedules")
    expect(page.get_by_role("heading", name="Schedules")).to_be_visible(timeout=30_000)
    rows = page.get_by_test_id("schedule-row")
    global_row = rows.filter(has_text=f"e2e-global-{tag}")
    expect(global_row).to_be_visible(timeout=30_000)
    expect(global_row).to_contain_text("hello_world")  # shows its agent target
    expect(rows.filter(has_text=f"e2e-conv-{tag}")).to_be_visible(timeout=30_000)
