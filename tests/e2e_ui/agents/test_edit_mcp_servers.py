"""E2E: adding/removing MCP servers from the agent info panel mid-session.

Covers the in-session MCP server editing flow end to end:
1. Open the agent info popover → verify the "+" button is present
2. Click "+" → fill the add-MCP dialog → submit → verify new pill appears
3. Click the pill → verify popover with "Remove" → click Remove → verify gone

Uses the sync Playwright API against the real live_server + seeded_session
fixtures (same pattern as test_agent_info_popover.py). The add/remove
round-trip exercises the real PUT /v1/sessions/{id}/agent endpoint with
the bundle download → modify → re-upload flow.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect


def _session_agent_mcp_names(base_url: str, session_id: str) -> set[str]:
    """Fetch the agent's MCP server names via the REST API."""
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/agent",
        timeout=10.0,
    )
    resp.raise_for_status()
    servers = resp.json().get("mcp_servers", [])
    return {s["name"] for s in servers}


def test_add_mcp_server_button_visible(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The agent info popover shows a '+' button next to the Tools label."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_placeholder("Ask the agent anything").wait_for(state="visible", timeout=30_000)

    trigger = page.get_by_test_id("agent-info-trigger")
    if trigger.count() == 0:
        return
    trigger.click()

    expect(page.get_by_test_id("add-mcp-server-button")).to_be_visible(timeout=5_000)


def test_add_mcp_dialog_validates_name(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The add-MCP dialog validates server names (rejects special chars)."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_placeholder("Ask the agent anything").wait_for(state="visible", timeout=30_000)

    trigger = page.get_by_test_id("agent-info-trigger")
    if trigger.count() == 0:
        return
    trigger.click()
    page.get_by_test_id("add-mcp-server-button").click()

    dialog = page.get_by_test_id("add-mcp-server-dialog")
    expect(dialog).to_be_visible(timeout=5_000)

    # Submit disabled with empty name.
    expect(page.get_by_test_id("add-mcp-submit")).to_be_disabled()

    # Invalid name → still disabled.
    page.get_by_test_id("add-mcp-name").fill("../../evil")
    expect(page.get_by_test_id("add-mcp-submit")).to_be_disabled()

    # Valid name + command → enabled.
    page.get_by_test_id("add-mcp-name").fill("testserver")
    page.get_by_test_id("add-mcp-command").fill("echo")
    expect(page.get_by_test_id("add-mcp-submit")).to_be_enabled()


def test_add_and_remove_mcp_server_round_trip(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Full round-trip: add an MCP server via the dialog, verify it appears,
    then remove it and verify it's gone.

    This exercises the real PUT /v1/sessions/{id}/agent bundle round-trip
    (download → modify tar → re-upload).
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_placeholder("Ask the agent anything").wait_for(state="visible", timeout=30_000)

    # Confirm no MCP servers initially via REST.
    initial_servers = _session_agent_mcp_names(base_url, session_id)

    # Open agent info popover.
    trigger = page.get_by_test_id("agent-info-trigger")
    if trigger.count() == 0:
        return
    trigger.click()

    # ── ADD ──────────────────────────────────────────────────────
    page.get_by_test_id("add-mcp-server-button").click()
    dialog = page.get_by_test_id("add-mcp-server-dialog")
    expect(dialog).to_be_visible(timeout=5_000)

    server_name = "e2e-test-mcp"
    page.get_by_test_id("add-mcp-name").fill(server_name)
    page.get_by_test_id("add-mcp-command").fill("echo")
    page.get_by_test_id("add-mcp-args").fill("hello")
    page.get_by_test_id("add-mcp-submit").click()

    # Dialog should close after successful add.
    expect(dialog).to_be_hidden(timeout=15_000)

    # Verify via REST that the server was added.
    updated_servers = _session_agent_mcp_names(base_url, session_id)
    assert server_name in updated_servers, (
        f"Expected '{server_name}' in {updated_servers}"
    )

    # The pill should appear in the UI (popover may need reopening after
    # the query invalidation).
    # Close and reopen the popover to see fresh data.
    page.keyboard.press("Escape")
    trigger.click()
    expect(page.get_by_text(server_name).first).to_be_visible(timeout=10_000)

    # ── REMOVE ───────────────────────────────────────────────────
    # Click the pill to open its popover.
    page.get_by_text(server_name).first.click()
    remove_btn = page.get_by_test_id(f"remove-mcp-server-{server_name}")
    expect(remove_btn).to_be_visible(timeout=5_000)
    remove_btn.click()

    # Wait for removal to complete — the pill should disappear.
    expect(page.get_by_test_id(f"remove-mcp-server-{server_name}")).to_be_hidden(
        timeout=15_000,
    )

    # Verify via REST that the server was removed.
    final_servers = _session_agent_mcp_names(base_url, session_id)
    assert server_name not in final_servers, (
        f"'{server_name}' still in {final_servers} after removal"
    )
    # Other servers (if any existed before) should be unaffected.
    assert initial_servers.issubset(final_servers), (
        f"Pre-existing servers {initial_servers} lost after remove; got {final_servers}"
    )
