"""The Agents tree follows sub-agent changes live without refetching per change.

A two-level tree (one child, one grandchild) is open in the Agents panel.
Status changes on either level must reach the rows from the session-updates
stream alone, with no ``/child_sessions`` request. A grandchild nothing
announces must still appear; renames, task summaries, turn ends, archive,
unarchive, and deletes must all reach the tree.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, Request, expect

from tests.e2e_ui.conftest import _server_state, seed_committed_turn

_ROW = '[data-testid="subagent-row"][data-child-session-id="{}"]'
_AVATAR = '[data-testid="subagent-status-avatar"]'
# One stream rescan is 4 s; leave room for a slow CI box.
_LIVE_TIMEOUT_MS = 20_000


def _start_codex_child(base_url: str, parent_id: str, name: str) -> str:
    """Register (or relabel) a Codex-native sub-agent and return its id."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{parent_id}/events",
        json={
            "type": "external_codex_subagent_start",
            "data": {
                "thread_id": f"thread_{parent_id}_{name.split()[0]}",
                "agent_nickname": name,
                "agent_role": "worker",
            },
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    child_id = resp.json()["child_session_id"]
    # Codex owns this child's execution; keep the test runner from waking it.
    httpx.patch(
        f"{base_url}/v1/sessions/{child_id}", json={"runner_id": ""}, timeout=10.0
    ).raise_for_status()
    return child_id


def _set_status(base_url: str, session_id: str, status: str, turn: str) -> None:
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": {"status": status, "response_id": turn}},
        timeout=30.0,
    ).raise_for_status()


def _set_archived(base_url: str, session_id: str, archived: bool) -> None:
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}", json={"archived": archived}, timeout=10.0
    ).raise_for_status()


def _require_tree_fields(base_url: str, session_id: str) -> None:
    """Skip on a server whose session rows predate the tree fields."""
    resp = httpx.get(
        f"{base_url}/v1/sessions", params={"limit": 100, "visibility": "all"}, timeout=10.0
    )
    resp.raise_for_status()
    row = next((r for r in resp.json()["data"] if r["id"] == session_id), None)
    if row is None or "child_session_count" not in row:
        pytest.skip("server rows carry no child_session_count")


def test_subagent_tree_streams_changes_without_refetching(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, parent_id = seeded_session
    _require_tree_fields(base_url, parent_id)
    child_id = _start_codex_child(base_url, parent_id, "planner")
    grandchild_id = _start_codex_child(base_url, child_id, "builder")
    created = [grandchild_id, child_id]
    try:
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.goto(f"{base_url}/c/{parent_id}?panel=agents")
        panel = page.get_by_role("complementary", name="Workspace")
        child_row = panel.locator(_ROW.format(child_id))
        grandchild_row = panel.locator(_ROW.format(grandchild_id))
        expect(child_row).to_be_visible(timeout=60_000)
        expect(grandchild_row).to_be_visible(timeout=30_000)

        list_requests: list[str] = []

        def _track(request: Request) -> None:
            if "/child_sessions" in request.url:
                list_requests.append(request.url)

        page.on("request", _track)

        # A grandchild starts working and keeps producing output: its row
        # updates from the stream, and no tree list is refetched.
        _set_status(base_url, grandchild_id, "running", "turn_g1")
        expect(grandchild_row.locator(_AVATAR)).to_have_attribute(
            "data-activity", "working", timeout=_LIVE_TIMEOUT_MS
        )
        seed_committed_turn(grandchild_id, prompt="build", reply="building", response_id="g1")
        _set_status(base_url, child_id, "running", "turn_c1")
        expect(child_row.locator(_AVATAR)).to_have_attribute(
            "data-activity", "working", timeout=_LIVE_TIMEOUT_MS
        )
        # Two more rescans for the output and any trailing frame to land.
        page.wait_for_timeout(9_000)
        assert list_requests == [], f"status-only changes refetched tree lists: {list_requests}"

        # Its turn ends: the row settles and picks up the new preview.
        _set_status(base_url, grandchild_id, "idle", "turn_g1")
        expect(grandchild_row.locator(_AVATAR)).to_have_attribute(
            "data-activity", "done", timeout=_LIVE_TIMEOUT_MS
        )

        # A relabel (Codex renames by nickname) reaches the row's name.
        _start_codex_child(base_url, child_id, "builder renamed")
        expect(grandchild_row).to_contain_text("builder renamed", timeout=_LIVE_TIMEOUT_MS)

        # A natively spawned grandchild is announced only by its parent's
        # child count; it still joins the tree.
        new_grandchild_id = _start_codex_child(base_url, child_id, "tester")
        created.insert(0, new_grandchild_id)
        new_row = panel.locator(_ROW.format(new_grandchild_id))
        expect(new_row).to_be_visible(timeout=_LIVE_TIMEOUT_MS)

        # A sub-agent row written straight to the store (nothing announces it)
        # appears, then its background task summary becomes its label.
        database_uri = _server_state.get("database_uri")
        if database_uri:
            from omnigent.stores.conversation_store.sqlalchemy_store import (
                SqlAlchemyConversationStore,
            )

            store = SqlAlchemyConversationStore(str(database_uri))
            child_row_entity = store.get_conversation(child_id)
            assert child_row_entity is not None
            plain = store.create_conversation(
                kind="sub_agent",
                title="worker:alpha",
                parent_conversation_id=child_id,
                agent_id=child_row_entity.agent_id,
            )
            created.insert(0, plain.id)
            plain_row = panel.locator(_ROW.format(plain.id))
            expect(plain_row).to_contain_text("alpha", timeout=_LIVE_TIMEOUT_MS)
            store.set_task_summary(plain.id, "Check the auth flow")
            expect(plain_row).to_contain_text("Check the auth flow", timeout=_LIVE_TIMEOUT_MS)

        # Archived elsewhere: the row leaves; unarchived: it comes back.
        _set_archived(base_url, new_grandchild_id, True)
        expect(new_row).to_have_count(0, timeout=_LIVE_TIMEOUT_MS)
        _set_archived(base_url, new_grandchild_id, False)
        expect(new_row).to_be_visible(timeout=_LIVE_TIMEOUT_MS)

        # A deleted grandchild leaves the tree.
        httpx.delete(f"{base_url}/v1/sessions/{grandchild_id}", timeout=10.0).raise_for_status()
        created.remove(grandchild_id)
        expect(grandchild_row).to_have_count(0, timeout=_LIVE_TIMEOUT_MS)
        expect(new_row).to_be_visible()
    finally:
        for session_id in created:
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
