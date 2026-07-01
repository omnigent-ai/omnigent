"""Regression lock-in for the phase-aware policy-evaluation timeout (#1026).

P1.9 is a NON-EDIT guard: the scaffold's ``TurnContext.evaluate_policy``
timeout default must stay phase-aware — on expiry, ``PHASE_TOOL_CALL`` fails
CLOSED (DENY) because it is the authoritative gate for connector-native MCP
tools, while advisory LLM phases and ``PHASE_TOOL_RESULT`` (the tool already
ran) fail OPEN (ALLOW). This test forces the timeout (no verdict ever
delivered) and asserts the defaults, so a future change to the constant or
the fallback can't silently flip a tool-call gate open.
"""

from __future__ import annotations

import asyncio

import pytest

import omnigent.runtime.harnesses._scaffold as scaffold
from omnigent.runtime.harnesses._scaffold import TurnContext


def _ctx() -> TurnContext:
    return TurnContext(
        response_id="resp_timeout",
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )


@pytest.mark.parametrize(
    ("phase", "expected_action"),
    [
        ("PHASE_TOOL_CALL", "POLICY_ACTION_DENY"),
        ("PHASE_LLM_REQUEST", "POLICY_ACTION_ALLOW"),
        ("PHASE_LLM_RESPONSE", "POLICY_ACTION_ALLOW"),
        ("PHASE_TOOL_RESULT", "POLICY_ACTION_ALLOW"),
    ],
)
async def test_timeout_default_is_phase_aware(
    monkeypatch: pytest.MonkeyPatch, phase: str, expected_action: str
) -> None:
    """On timeout with no verdict, TOOL_CALL fails closed; advisory phases open."""
    monkeypatch.setattr(scaffold, "_POLICY_EVAL_TIMEOUT_S", 0.0)
    ctx = _ctx()
    # No verdict is ever delivered, so wait_for(timeout=0) expires immediately.
    verdict = await ctx.evaluate_policy(f"poleval_{phase}", phase, {})
    assert verdict.action == expected_action, (phase, verdict)
    # The parked future was cleaned up.
    assert not ctx._pending_policy_evaluations
