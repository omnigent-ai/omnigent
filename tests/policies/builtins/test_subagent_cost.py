"""
Tests for the built-in subagent cost-budget policy
(:mod:`omnigent.policies.builtins.cost` — the ``subagent_cost_budget``
factory), focused on the over-cap verdict.

The subagent budget is typically attached by the ORCHESTRATING MODEL at
spawn time (``sys_session_send``'s ``cost_budget``), not by the user, so
its block-all hard cap (``expensive_models`` omitted/empty) must not
hard-DENY a user who was never warned the budget exists:

- **block-all cap** (the spawn path's max-only shape): the first
  over-cap gate ASKs to lift the cap; approval is recorded in
  ``session_state`` (applied on accept) and makes later gates ALLOW; a
  decline records nothing so the next gate re-asks.
- **downgrade gate** (explicit non-empty ``expensive_models``): the
  designed DENY-on-expensive-model behavior is unchanged — that block is
  escapable by switching models, so it needs no approval valve.
"""

from __future__ import annotations

from typing import Any

from omnigent.policies.builtins.cost import (
    _SUBAGENT_ASK_APPROVED_KEY,
    _SUBAGENT_OVER_BUDGET_APPROVED_KEY,
    subagent_cost_budget,
)
from omnigent.policies.schema import PolicyEvent


def _tool(
    cost: float | None,
    *,
    model: str | None = "databricks-claude-opus-4-8",
    session_state: dict[str, Any] | None = None,
) -> PolicyEvent:
    """
    Build a ``tool_call`` :class:`PolicyEvent` with a subtree cost.

    :param cost: ``total_cost_usd`` under ``context.subtree_usage``,
        e.g. ``37.86``. ``None`` omits the field (unpriced case).
    :param model: Active model under ``context.model``; ``None`` for the
        undeterminable-model case.
    :param session_state: Optional persisted state, e.g.
        ``{_SUBAGENT_OVER_BUDGET_APPROVED_KEY: 3.0}``.
    :returns: A ``tool_call`` event dict.
    """
    usage: dict[str, Any] = {} if cost is None else {"total_cost_usd": cost}
    return {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {}},
        "context": {"actor": {}, "subtree_usage": usage, "model": model},
        "session_state": session_state or {},
    }


def _request(
    cost: float,
    *,
    session_state: dict[str, Any] | None = None,
) -> PolicyEvent:
    """
    Build a ``request`` :class:`PolicyEvent` with a subtree cost.

    :param cost: ``total_cost_usd`` under ``context.subtree_usage``.
    :param session_state: Optional persisted state.
    :returns: A ``request`` event dict.
    """
    return {
        "type": "request",
        "target": None,
        "data": "please keep going",
        "context": {"actor": {}, "subtree_usage": {"total_cost_usd": cost}, "model": "opus"},
        "session_state": session_state or {},
    }


# ── Block-all cap: the ASK valve ────────────────────────────────────────


def test_block_all_cap_asks_instead_of_denying() -> None:
    """The first over-cap gate of a max-only budget ASKs, not DENYs.

    This is the spawn path's exact shape: ``max_cost_usd`` only, no
    ``ask_thresholds_usd``, no ``expensive_models`` — previously an
    immediate un-warned hard DENY at Nx the cap.
    """
    policy = subagent_cost_budget(max_cost_usd=3.0)
    verdict = policy(_tool(37.86))
    assert verdict["result"] == "ASK"
    assert "$3.00" in verdict["reason"]
    assert "$37.86" in verdict["reason"]
    # The approval write is carried on the ASK, applied only on accept.
    assert verdict["state_updates"] == [
        {"key": _SUBAGENT_OVER_BUDGET_APPROVED_KEY, "action": "set", "value": 3.0},
    ]


def test_block_all_cap_asks_on_request_phase_too() -> None:
    """The request phase (whole-turn gate) gets the same ASK valve."""
    policy = subagent_cost_budget(max_cost_usd=3.0)
    verdict = policy(_request(5.0))
    assert verdict["result"] == "ASK"


def test_block_all_cap_asks_regardless_of_model() -> None:
    """Block-all gates every model, so the ASK fires on any/unknown model."""
    policy = subagent_cost_budget(max_cost_usd=3.0)
    assert policy(_tool(4.0, model="cheap-haiku"))["result"] == "ASK"
    assert policy(_tool(4.0, model=None))["result"] == "ASK"


def test_approved_cap_allows_from_then_on() -> None:
    """A recorded over-cap approval lifts the cap for later gates."""
    policy = subagent_cost_budget(max_cost_usd=3.0)
    state = {_SUBAGENT_OVER_BUDGET_APPROVED_KEY: 3.0}
    assert policy(_tool(37.86, session_state=state)) == {"result": "ALLOW"}
    assert policy(_request(50.0, session_state=state)) == {"result": "ALLOW"}


def test_declined_cap_reasks_next_gate() -> None:
    """A decline records nothing, so the next gate re-asks (fail closed)."""
    policy = subagent_cost_budget(max_cost_usd=3.0)
    # No approval in state — every gate keeps asking rather than allowing.
    assert policy(_tool(10.0))["result"] == "ASK"
    assert policy(_tool(11.0))["result"] == "ASK"


def test_stale_lower_approval_does_not_lift_a_higher_cap() -> None:
    """An approval recorded for a smaller cap does not cover a larger one."""
    policy = subagent_cost_budget(max_cost_usd=5.0)
    state = {_SUBAGENT_OVER_BUDGET_APPROVED_KEY: 3.0}
    assert policy(_tool(6.0, session_state=state))["result"] == "ASK"


def test_malformed_approval_state_is_ignored() -> None:
    """A malformed persisted approval value must not crash or lift the cap."""
    policy = subagent_cost_budget(max_cost_usd=3.0)
    for bad in (["3.0"], {"usd": 3.0}, "not-a-number", None):
        verdict = policy(_tool(4.0, session_state={_SUBAGENT_OVER_BUDGET_APPROVED_KEY: bad}))
        assert verdict["result"] == "ASK", bad


def test_below_cap_allows() -> None:
    """Spend under the cap abstains (ALLOW) — no valve, no prompt."""
    policy = subagent_cost_budget(max_cost_usd=3.0)
    assert policy(_tool(1.0)) == {"result": "ALLOW"}


# ── Explicit expensive_models: the downgrade gate is unchanged ──────────


def test_explicit_expensive_models_keeps_deny_over_cap() -> None:
    """A named-tier budget still hard-DENYs on an expensive model.

    That block is escapable (switch to a cheaper model), so it keeps the
    designed DENY rather than the ASK valve.
    """
    policy = subagent_cost_budget(max_cost_usd=3.0, expensive_models=["opus"])
    verdict = policy(_tool(4.0, model="databricks-claude-opus-4-8"))
    assert verdict["result"] == "DENY"
    assert "subagent cost-budget" in verdict["reason"]


def test_explicit_expensive_models_allows_cheaper_model_over_cap() -> None:
    """Over the cap on a non-listed model, the downgrade gate is satisfied."""
    policy = subagent_cost_budget(max_cost_usd=3.0, expensive_models=["opus"])
    assert policy(_tool(4.0, model="cheap-haiku")) == {"result": "ALLOW"}


# ── Soft checkpoints keep their local approval key ──────────────────────


def test_soft_checkpoint_asks_with_local_key() -> None:
    """Crossing a soft checkpoint ASKs and records the LOCAL subagent key."""
    policy = subagent_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    verdict = policy(_tool(2.5))
    assert verdict["result"] == "ASK"
    assert verdict["state_updates"] == [
        {"key": _SUBAGENT_ASK_APPROVED_KEY, "action": "set", "value": 2.0},
    ]


def test_approved_soft_checkpoint_does_not_reprompt() -> None:
    """An approved checkpoint stays quiet below the cap."""
    policy = subagent_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    state = {_SUBAGENT_ASK_APPROVED_KEY: 2.0}
    assert policy(_tool(2.5, session_state=state)) == {"result": "ALLOW"}
