"""Codex native child-spawn bridge to Agents-rail end-to-end coverage."""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from tests.e2e_ui.conftest import open_right_rail

_PARENT_THREAD = "thread_parent"
_CHILD_THREAD = "thread_child"


def _spawn_and_complete_events() -> list[dict]:
    """Codex events registering a child and running its turn to completion."""
    return [
        {
            "method": "item/completed",
            "params": {
                "threadId": _PARENT_THREAD,
                "turnId": "turn_parent",
                "item": {
                    "type": "subAgentActivity",
                    "id": "activity_1",
                    "kind": "started",
                    "agentThreadId": _CHILD_THREAD,
                    "agentPath": "root/researcher",
                },
            },
        },
        {
            "method": "turn/started",
            "params": {
                "threadId": _CHILD_THREAD,
                "turn": {"id": "turn_child", "status": "inProgress"},
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": _CHILD_THREAD,
                "turn": {"id": "turn_child", "status": "completed", "items": []},
            },
        },
    ]


def _stale_running_snapshot_event() -> dict:
    """A later parent spawn item whose ``agentsStates`` still lists the child as running."""
    return {
        "method": "item/completed",
        "params": {
            "threadId": _PARENT_THREAD,
            "turnId": "turn_parent_2",
            "item": {
                "type": "collabAgentToolCall",
                "id": "collab_1",
                "tool": "spawnAgent",
                "senderThreadId": _PARENT_THREAD,
                "receiverThreadIds": [_CHILD_THREAD],
                "agentsStates": {_CHILD_THREAD: {"status": "running"}},
            },
        },
    }


async def _forward_events(
    base_url: str,
    session_id: str,
    events: list[dict],
    state: codex_native_forwarder._CodexForwarderState,
) -> None:
    """Drive Codex events through the real forwarder into the live server."""
    tracker = codex_native_forwarder._CodexElicitationTaskTracker()
    async with httpx.AsyncClient(base_url=base_url) as client:
        for event in events:
            await codex_native_forwarder._handle_event(
                client,
                session_id=session_id,
                bridge_dir=Path(),
                event=event,
                usage_coalescer=codex_native_forwarder._SessionUsageCoalescer(client, session_id),
                elicitation_tracker=tracker,
                expected_thread_id=_PARENT_THREAD,
                forwarder_state=state,
            )
    await tracker.close()


def _forward_events_sync(
    base_url: str,
    session_id: str,
    events: list[dict],
    state: codex_native_forwarder._CodexForwarderState,
) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, _forward_events(base_url, session_id, events, state)).result()


def _assert_child_completed(base_url: str, session_id: str) -> None:
    children = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/child_sessions", timeout=10.0
    ).json()["data"]
    assert [child["session_name"] for child in children] == [_CHILD_THREAD]
    assert children[0]["busy"] is False, children[0]
    assert children[0]["current_task_status"] == "completed", children[0]


def _expect_child_row_done(page: Page, base_url: str, session_id: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    child_row = rail.locator('[data-testid="subagent-row"]')
    expect(child_row).to_have_count(1, timeout=30_000)
    expect(child_row).to_contain_text("Codex")
    expect(child_row.get_by_test_id("subagent-status-avatar")).to_have_attribute(
        "aria-label", "Done", timeout=10_000
    )


def test_codex_child_stays_done_in_agents_rail_after_stale_running_snapshot(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A spawned child shows "Done" once finished and keeps it after a stale snapshot."""
    base_url, session_id = seeded_session
    state = codex_native_forwarder._CodexForwarderState(parent_session_id=session_id)
    _forward_events_sync(base_url, session_id, _spawn_and_complete_events(), state)
    _assert_child_completed(base_url, session_id)
    _expect_child_row_done(page, base_url, session_id)

    # A later parent spawn item still lists the finished child as running.
    _forward_events_sync(base_url, session_id, [_stale_running_snapshot_event()], state)
    _assert_child_completed(base_url, session_id)
    _expect_child_row_done(page, base_url, session_id)
