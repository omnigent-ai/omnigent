"""Codex native child-spawn bridge to Agents-rail end-to-end coverage."""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from omnigent import codex_native_forwarder
from tests.e2e_ui.conftest import open_right_rail


async def _forward_spawn_activity(base_url: str, session_id: str) -> None:
    """Drive a native Codex spawn and child completion through the server."""
    state = codex_native_forwarder._CodexForwarderState(parent_session_id=session_id)
    tracker = codex_native_forwarder._CodexElicitationTaskTracker()
    async with httpx.AsyncClient(base_url=base_url) as client:
        events = [
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread_parent",
                    "turnId": "turn_parent",
                    "item": {
                        "type": "subAgentActivity",
                        "id": "activity_1",
                        "kind": "started",
                        "agentThreadId": "thread_child",
                        "agentPath": "root/researcher",
                    },
                },
            },
            {
                "method": "turn/started",
                "params": {
                    "threadId": "thread_child",
                    "turn": {"id": "turn_child", "status": "inProgress"},
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread_child",
                    "turn": {"id": "turn_child", "status": "completed", "items": []},
                },
            },
        ]
        for event in events:
            await codex_native_forwarder._handle_event(
                client,
                session_id=session_id,
                bridge_dir=Path(),
                event=event,
                usage_coalescer=codex_native_forwarder._SessionUsageCoalescer(client, session_id),
                elicitation_tracker=tracker,
                expected_thread_id="thread_parent",
                forwarder_state=state,
            )
    await tracker.close()


def test_codex_spawn_activity_appears_in_agents_rail(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A Codex native spawn event creates the child row rendered by the UI."""
    base_url, session_id = seeded_session
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, _forward_spawn_activity(base_url, session_id)).result()

    children = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/child_sessions", timeout=10.0
    ).json()["data"]
    assert [child["session_name"] for child in children] == ["thread_child"]
    assert children[0]["busy"] is False
    assert children[0]["current_task_status"] == "completed"

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    child_row = rail.locator('[data-testid="subagent-row"]')
    expect(child_row).to_have_count(1, timeout=30_000)
    expect(child_row).to_contain_text("Codex")
    expect(child_row.get_by_test_id("subagent-status-dot")).to_have_attribute("aria-label", "Done")
