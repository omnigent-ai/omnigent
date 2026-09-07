"""E2E: a spawn-attached cost budget warns in the chat instead of hard-blocking.

The sub-agent spawn path (``sys_session_send``'s ``cost_budget``) attaches a
max-only ``subagent_cost_budget`` the user never set. Before the fix, the
first gate after the cap was crossed hard-DENYed every model and tool call
("spend $37.86 reached the $3.00 limit") with no warning ever shown — and no
way to continue. Now the first over-cap gate parks a server-side ASK, which
the web chat renders as a pending ``ApprovalCard``; approving lifts the cap
and the gated call proceeds.

This test drives the fixed journey end-to-end in the real UI: seed spend far
over the cap, attach the exact spawn-shaped policy payload, fire the gated
tool-call evaluation (the same ``POST /policies/evaluate`` a native hook
posts), then watch the approval card appear, approve it, and assert the gate
collapses to ALLOW. No LLM in the loop (the gate is fired directly, like the
synthetic permission-hook tests), so it runs on every PR.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_CARD_TIMEOUT_MS = 20_000

# The exact policy payload the sys_session_send spawn path posts to the child
# session when the orchestrating model passes a max-only cost_budget.
_SPAWN_BUDGET_PAYLOAD = {
    "name": "__subagent_cost_budget",
    "type": "python",
    "handler": "omnigent.policies.builtins.cost.subagent_cost_budget",
    "factory_params": {"max_cost_usd": 3.0},
    "enabled": True,
}


def _seed_session_usage(session_id: str, usage: dict) -> None:
    """Write cumulative usage straight into the spawned server's store.

    :param session_id: Session to seed, e.g. ``"conv_abc123"``.
    :param usage: Usage dict, e.g. ``{"total_cost_usd": 37.86}``.
    :raises RuntimeError: When running against ``--ui-base-url`` (no
        local database to seed).
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from tests.e2e_ui.conftest import _server_state

    database_uri = _server_state.get("database_uri")
    if not database_uri:
        raise RuntimeError(
            "seeding needs the spawned server's database; it is "
            "unavailable when running against --ui-base-url."
        )
    SqlAlchemyConversationStore(str(database_uri)).set_session_usage(session_id, usage)


@pytest.mark.timeout(120)
def test_over_cap_budget_shows_approval_card_and_unblocks(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Over-cap gate → pending approval card → Approve → gate ALLOWs."""
    base_url, session_id = seeded_session

    # The user worked normally; spend accrued far past the (later-attached)
    # cap before any budget existed.
    _seed_session_usage(session_id, {"total_cost_usd": 37.86})

    # A $3 max-only budget lands without user action — the exact payload the
    # spawn path posts.
    attach = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/policies",
        json=_SPAWN_BUDGET_PAYLOAD,
        timeout=10.0,
    )
    assert attach.status_code < 400, f"policy attach failed: {attach.status_code} {attach.text}"

    # Fire the next gated tool call (what a native PreToolUse hook posts).
    # The server parks it on the ASK until the human resolves the card.
    result_holder: dict = {}

    def _evaluate() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/policies/evaluate",
                json={
                    "event": {
                        "type": "PHASE_TOOL_CALL",
                        "target": "",
                        "data": {"name": "Bash", "arguments": {"command": "ls"}},
                        "context": {},
                    },
                },
                timeout=90.0,
            )
            resp.raise_for_status()
            result_holder["response"] = resp.json()
        except Exception as exc:  # surfaced after the assertions below
            result_holder["error"] = exc

    gate_thread = threading.Thread(target=_evaluate, daemon=True)
    gate_thread.start()
    # Let the server park the elicitation before the SPA renders it.
    page.wait_for_timeout(500)

    page.goto(f"{base_url}/c/{session_id}")

    # The fixed behavior: a pending approval card naming the cap, instead of
    # the old un-warned hard block.
    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_CARD_TIMEOUT_MS)
    expect(card).to_contain_text("$3.00")
    expect(card).to_contain_text("$37.86")

    card.get_by_role("button", name="Approve", exact=True).click()

    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=_CARD_TIMEOUT_MS)
    expect(responded.get_by_text("Approved", exact=False).first).to_be_visible()

    # Approval collapses the parked gate to a hard ALLOW.
    gate_thread.join(timeout=60)
    assert not gate_thread.is_alive(), "policy gate never settled after approval"
    if "error" in result_holder:
        raise AssertionError(f"gate thread failed: {result_holder['error']}")
    assert result_holder["response"]["result"] == "POLICY_ACTION_ALLOW", result_holder["response"]

    # The lifted cap is remembered: the next gate ALLOWs without re-asking.
    deadline = time.monotonic() + 30.0
    follow_up: dict = {}
    while time.monotonic() < deadline:
        resp = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/policies/evaluate",
            json={
                "event": {
                    "type": "PHASE_TOOL_CALL",
                    "target": "",
                    "data": {"name": "Bash", "arguments": {"command": "ls"}},
                    "context": {},
                },
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        follow_up = resp.json()
        if follow_up.get("result") == "POLICY_ACTION_ALLOW":
            break
        time.sleep(0.5)
    assert follow_up.get("result") == "POLICY_ACTION_ALLOW", follow_up
