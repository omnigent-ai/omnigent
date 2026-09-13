"""E2E: Web exposes and returns Codex MCP persistence choices.

A synthetic native-Codex hook parks the same MCP approval request the Codex
TUI receives. The browser must render every advertised persistence choice,
and choosing the session scope must reach the blocked hook as
``_meta.persist = "session"``.

No real model or Codex binary is required; ``seeded_session`` supplies the
live server/session while the hook exercises the production protocol adapter.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_MOCK_ELICITATION_TIMEOUT_MS = 15_000


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.25) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


@pytest.mark.timeout(90)
def test_codex_mcp_session_approval_round_trip(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Advertised once/session/always choices render and session reaches Codex."""
    base_url, session_id = seeded_session
    result_holder: dict = {}
    payload = {
        "id": 8,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_e2e",
            "turnId": "turn_e2e",
            "serverName": "omnigent",
            "mode": "form",
            "message": 'Allow the omnigent MCP server to run tool "sys_read_inbox"?',
            "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {
                "codex_approval_kind": "mcp_tool_call",
                "persist": ["session", "always"],
            },
        },
    }

    def _post_hook() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/codex-elicitation-request",
                json=payload,
                timeout=60.0,
            )
            resp.raise_for_status()
            result_holder["response"] = resp.json()
        except Exception as exc:
            result_holder["error"] = exc

    hook_thread = threading.Thread(target=_post_hook, daemon=True)
    hook_thread.start()
    _wait_for(lambda: bool(_pending_elicitations(base_url, session_id)))

    page.goto(f"{base_url}/c/{session_id}")

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').filter(
        has_text="sys_read_inbox"
    )
    expect(card.first).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    expect(card.get_by_role("button", name="Approve", exact=True)).to_be_visible()
    expect(card.get_by_role("button", name="Approve for this session", exact=True)).to_be_visible()
    expect(card.get_by_role("button", name="Always allow", exact=True)).to_be_visible()
    expect(card.get_by_role("button", name="Reject", exact=True)).to_be_visible()

    card.get_by_role("button", name="Approve for this session", exact=True).click()

    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').filter(
        has_text="Approved for this session"
    )
    expect(responded.first).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)

    hook_thread.join(timeout=30)
    if "error" in result_holder:
        raise AssertionError(f"hook thread failed: {result_holder['error']}") from result_holder[
            "error"
        ]
    assert result_holder["response"] == {
        "action": "accept",
        "content": None,
        "_meta": {"persist": "session"},
    }
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))
