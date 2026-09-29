"""Claude Code sub-agent naming, from the forwarder's event to the rail row."""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_HEX_SUBAGENT_ID = "a09d1dd1d8dbc0151"
_NAMESPACED_SUBAGENT_ID = "a361e6a6aa05689cb"


def _register_subagent(
    base_url: str,
    session_id: str,
    *,
    subagent_id: str,
    agent_type: str,
    description: str,
) -> None:
    """Post the forwarder's ``external_subagent_start`` for one Task spawn."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_subagent_start",
            "data": {
                "subagent_id": subagent_id,
                "agent_type": agent_type,
                "description": description,
                "tool_use_id": f"toolu_{subagent_id}",
            },
        },
        timeout=10.0,
    )
    assert resp.status_code in (200, 202), resp.text


def test_claude_subagent_rows_show_names_not_hex_ids(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The rail names Claude sub-agents by description, never by their id.

    Their conversation title is ``"<agentType>:<subagentId>"`` — a
    per-parent uniqueness key. Rendering a slice of it put bare hex ids in
    the rail, so a wave of parallel workers read as identical rows. The
    second row also carries a plugin-namespaced agent type, whose own
    colon used to leak the id half through the same split.
    """
    base_url, session_id = seeded_session
    # Mark the parent as a Claude Code session so the sub-agent event is
    # accepted for it, the way ``omnigent claude`` stamps a real one.
    patched = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.wrapper": "claude-code-native-ui"}},
        timeout=10.0,
    )
    assert patched.status_code == 200, patched.text

    _register_subagent(
        base_url,
        session_id,
        subagent_id=_HEX_SUBAGENT_ID,
        agent_type="general-purpose",
        description="wave-worker-696",
    )
    _register_subagent(
        base_url,
        session_id,
        subagent_id=_NAMESPACED_SUBAGENT_ID,
        agent_type="rpw-published:debug-lead",
        description="",
    )

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    rows = rail.locator('[data-testid="subagent-row"]')
    expect(rows).to_have_count(2, timeout=30_000)

    expect(rows.filter(has_text="wave-worker-696")).to_have_count(1)
    expect(rows.filter(has_text="debug-lead")).to_have_count(1)
    expect(rows.filter(has_text=_HEX_SUBAGENT_ID)).to_have_count(0)
    expect(rows.filter(has_text=_NAMESPACED_SUBAGENT_ID)).to_have_count(0)
