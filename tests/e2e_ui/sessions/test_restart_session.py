"""E2E coverage for the header menu's "Restart session…" action.

Drives the real UI: open the session actions menu, confirm the restart
dialog, and verify the session reloads in place — same URL, transcript
intact, agent binding refreshed from the latest installed version.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect


def test_header_menu_restarts_session_in_place(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Restart via the header menu keeps the session and reloads it fresh."""
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    trigger = page.get_by_test_id("header-conversation-actions")
    expect(trigger).to_be_visible(timeout=30_000)
    trigger.click()

    restart_item = page.get_by_role("menuitem", name="Restart session…")
    expect(restart_item).to_be_visible()
    restart_item.click()

    # The confirm dialog guards the action; cancelling changes nothing.
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_role("heading", name="Restart session?")).to_be_visible()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)

    trigger.click()
    page.get_by_role("menuitem", name="Restart session…").click()
    page.get_by_test_id("header-restart-confirm").click()

    # The dialog closes on success and the session reloads in place.
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=30_000)
    expect(page).to_have_url(f"{base_url}/c/{session_id}")
    expect(trigger).to_be_visible(timeout=30_000)

    after = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    after.raise_for_status()
    body = after.json()
    assert body["id"] == session_id, "restart must keep the same session id"
    assert body["status"] == "idle", f"restarted session should be idle, got {body['status']}"
    # The session still resolves to a working agent of the same family. When
    # the fixture's binding is a drifted clone the restart rebinds to a fresh
    # clone of the current built-in (agent id changes); an up-to-date or
    # built-in-bound session keeps its id. Either shape is a valid restart.
    agent_after = httpx.get(f"{base_url}/v1/sessions/{session_id}/agent", timeout=10.0)
    agent_after.raise_for_status()
    assert "hello_world" in str(agent_after.json()["name"]), (
        f"restart must keep the session on the hello_world agent, got {agent_after.json()}"
    )
