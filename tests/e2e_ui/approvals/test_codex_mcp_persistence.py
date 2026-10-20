"""E2E: Web exposes and returns Codex persistence choices.

A synthetic native-Codex hook parks the same approval request the Codex TUI
receives. The browser must render the session-scoped choice Codex offers, and
choosing it must reach the blocked hook in Codex's protocol shape: MCP tool
calls answer ``_meta.persist = "session"``, file changes answer
``acceptForSession``.

No real model or Codex binary is required; ``seeded_session`` supplies the
live server/session while the hook exercises the production protocol adapter.
"""

from __future__ import annotations

import threading

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.polling import poll_until

_APPROVAL_CARD = '[data-testid="approval-card"]'
_MOCK_ELICITATION_TIMEOUT_MS = 15_000


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _park_codex_hook(
    base_url: str, session_id: str, payload: dict
) -> tuple[threading.Thread, dict]:
    """POST the hook on a background thread and wait until its card is pending."""
    result_holder: dict = {}

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
    poll_until(lambda: bool(_pending_elicitations(base_url, session_id)), interval_s=0.25)
    return hook_thread, result_holder


def _hook_response(hook_thread: threading.Thread, result_holder: dict) -> dict:
    hook_thread.join(timeout=60)
    assert not hook_thread.is_alive(), "Codex hook did not return after the verdict"
    if "error" in result_holder:
        raise AssertionError(f"hook thread failed: {result_holder['error']}") from result_holder[
            "error"
        ]
    return result_holder["response"]


@pytest.mark.timeout(90)
def test_codex_mcp_session_approval_round_trip(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Advertised once/session/always choices render and session reaches Codex."""
    base_url, session_id = seeded_session
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
    hook_thread, result_holder = _park_codex_hook(base_url, session_id, payload)

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

    assert _hook_response(hook_thread, result_holder) == {
        "action": "accept",
        "content": None,
        "_meta": {"persist": "session"},
    }
    poll_until(lambda: not _pending_elicitations(base_url, session_id), interval_s=0.25)


@pytest.mark.timeout(90)
def test_codex_file_change_session_approval_round_trip(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A file-change card offers Codex's session-scoped approve and returns acceptForSession."""
    base_url, session_id = seeded_session
    payload = {
        "id": 21,
        "method": "item/fileChange/requestApproval",
        "params": {
            "threadId": "thread_e2e",
            "turnId": "turn_e2e",
            "itemId": "item_patch_e2e",
            "startedAtMs": 1,
            "reason": None,
            "grantRoot": None,
        },
    }
    hook_thread, result_holder = _park_codex_hook(base_url, session_id, payload)

    page.goto(f"{base_url}/c/{session_id}")

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').filter(has_text="modify files")
    expect(card.first).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    expect(card.get_by_role("button", name="Approve", exact=True)).to_be_visible()
    expect(card.get_by_role("button", name="Reject", exact=True)).to_be_visible()
    session_approve = card.get_by_role("button", name="Approve for this session", exact=True)
    expect(
        session_approve, "file-change card offers no session-scoped approve choice"
    ).to_be_visible()

    session_approve.click()

    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').filter(
        has_text="Approved for this session"
    )
    expect(responded.first).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    assert _hook_response(hook_thread, result_holder) == {"decision": "acceptForSession"}
    poll_until(lambda: not _pending_elicitations(base_url, session_id), interval_s=0.25)
