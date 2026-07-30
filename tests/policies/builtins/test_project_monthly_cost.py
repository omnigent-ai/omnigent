"""Tests for the project monthly cost-budget policy.

``project_monthly_cost_budget`` gates the ``request`` and ``tool_call``
phases on the session's PROJECT's cumulative spend for the current UTC
calendar month, read from ``event["context"]["project_monthly_cost"]``
(injected by the engine). Same ASK/DENY/downgrade logic as
``user_daily_cost_budget`` (see ``PLAN.md``, closes #1662), but:

- the budget is the project's monthly spend, not a user's daily spend;
- an approved soft checkpoint is read from / recorded to the monthly
  store (``ask_approved_usd``) rather than ``session_state``, via the
  reserved ``PROJECT_MONTHLY_ASK_APPROVED_STATE_KEY`` state-update the
  engine routes to that store.

These exercise the policy callable directly with synthetic events — the
engine wiring (injection + state-update routing) and the automatic
synthesis of this policy from a project's ``budget_config`` are covered
by ``tests/runtime/policies/test_project_budget_wiring.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.policies.builtins.cost import project_monthly_cost_budget
from omnigent.policies.schema import PROJECT_MONTHLY_ASK_APPROVED_STATE_KEY, PolicyEvent


def _tool(
    cost: float | None,
    *,
    ask_approved: float = 0.0,
    model: str | None = "databricks-claude-opus-4-8",
    harness: str | None = None,
    project_id: str | None = None,
) -> PolicyEvent:
    """
    Build a ``tool_call`` event carrying the project's monthly cost rollup.

    :param cost: ``cost_usd`` under ``context.project_monthly_cost``, e.g.
        ``12.5``. ``None`` omits the field (unpriced / no-project case).
    :param ask_approved: ``ask_approved_usd`` under
        ``context.project_monthly_cost`` — the highest checkpoint the
        project owner already approved this month, e.g. ``10.0``.
    :param model: Active model under ``context.model``; defaults to an
        expensive (Opus) model. ``None`` is the undeterminable case.
    :param harness: ``context.harness``, e.g. ``"codex-native"``;
        ``None`` is the web / API / unstamped case.
    :param project_id: ``project_id`` under ``context.project_monthly_cost``
        — the project the rollup belongs to. ``None`` omits it (unfiled
        session).
    :returns: A ``tool_call`` event dict.
    """
    monthly: dict[str, Any] = (
        {} if cost is None else {"cost_usd": cost, "ask_approved_usd": ask_approved}
    )
    if project_id is not None:
        monthly["project_id"] = project_id
    return {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {}},
        "context": {
            "actor": {},
            "project_monthly_cost": monthly,
            "model": model,
            "harness": harness,
        },
        "session_state": {},
    }


def test_below_ask_threshold_allows() -> None:
    """Monthly spend under the lowest checkpoint abstains (ALLOW)."""
    policy = project_monthly_cost_budget(max_cost_usd=50.0, ask_thresholds_usd=[25.0])
    assert policy(_tool(10.0)) == {"result": "ALLOW"}


@pytest.mark.parametrize("phase", ["response", "tool_result", "llm_request", "llm_response"])
def test_non_gated_phase_allows(phase: str) -> None:
    """Non-gated phases abstain even over budget (request/tool_call ARE gated)."""
    policy = project_monthly_cost_budget(max_cost_usd=50.0)
    event: PolicyEvent = {
        "type": phase,
        "target": None,
        "data": "x",
        "context": {"project_monthly_cost": {"cost_usd": 99.99}, "model": "opus"},
        "session_state": {},
    }
    assert policy(event) == {"result": "ALLOW"}


def test_request_phase_over_budget_on_expensive_model_denies() -> None:
    """Over the hard monthly limit on an expensive model DENYs at the request phase.

    The monthly gate fires before the LLM turn too, so a text-only turn
    counts against the monthly budget. The reason must be the user-facing
    variant (no tool-call directive), since a request-phase DENY is shown
    straight to the user.
    """
    policy = project_monthly_cost_budget(max_cost_usd=50.0)
    event: PolicyEvent = {
        "type": "request",
        "target": None,
        "data": "please run the build",
        "context": {
            "actor": {},
            "project_monthly_cost": {"cost_usd": 60.0, "ask_approved_usd": 0.0},
            "model": "databricks-claude-opus-4-8",
        },
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "DENY"
    assert "60.00" in result["reason"]
    assert "monthly" in result["reason"].lower()
    # User-facing phrasing only — no tool-call directive leaks through.
    assert "re-issue the tool call" not in result["reason"]


def test_request_phase_soft_checkpoint_asks_and_records_monthly_key() -> None:
    """Crossing a monthly checkpoint ASKs at the request phase → ASK + monthly key."""
    policy = project_monthly_cost_budget(max_cost_usd=50.0, ask_thresholds_usd=[25.0])
    event: PolicyEvent = {
        "type": "request",
        "target": None,
        "data": "please run the build",
        "context": {
            "actor": {},
            "project_monthly_cost": {"cost_usd": 25.0, "ask_approved_usd": 0.0},
            "model": "databricks-claude-opus-4-8",
        },
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "ASK"
    assert result["state_updates"] == [
        {"key": PROJECT_MONTHLY_ASK_APPROVED_STATE_KEY, "action": "set", "value": 25.0},
    ]


def test_zero_or_missing_monthly_cost_allows() -> None:
    """No monthly cost recorded (no project / unpriced) → never trips."""
    policy = project_monthly_cost_budget(max_cost_usd=50.0, ask_thresholds_usd=[10.0])
    # cost=None omits the field entirely → reads as 0.0.
    assert policy(_tool(None)) == {"result": "ALLOW"}


def test_crossing_a_checkpoint_asks_and_records_monthly_key() -> None:
    """Crossing a monthly checkpoint (unapproved) → ASK + reserved monthly state key.

    The ASK must carry a ``state_updates`` SET on
    ``PROJECT_MONTHLY_ASK_APPROVED_STATE_KEY`` (NOT the session key) so
    the engine routes the approval to the per-project+month store — that's
    what makes an approval persist across the project's other sessions
    this month. A missing / wrong key would re-prompt every session.
    """
    policy = project_monthly_cost_budget(max_cost_usd=50.0, ask_thresholds_usd=[20.0, 40.0])
    result = policy(_tool(20.0))  # exactly at the first checkpoint — `>=`
    assert result["result"] == "ASK"
    assert result["state_updates"] == [
        {"key": PROJECT_MONTHLY_ASK_APPROVED_STATE_KEY, "action": "set", "value": 20.0},
    ]


def test_approved_checkpoint_from_monthly_store_does_not_reprompt() -> None:
    """Approval read from context.project_monthly_cost.ask_approved_usd suppresses re-ask.

    With ask_approved=20.0, a $30 monthly-spend tool call is silent;
    reaching the next checkpoint ($40) ASKs again.
    """
    policy = project_monthly_cost_budget(max_cost_usd=50.0, ask_thresholds_usd=[20.0, 40.0])
    # Already approved past $20 this month (possibly from a different session) → silent.
    assert policy(_tool(30.0, ask_approved=20.0)) == {"result": "ALLOW"}
    # Crossing the next checkpoint prompts again.
    result = policy(_tool(40.0, ask_approved=20.0))
    assert result["result"] == "ASK"
    assert result["state_updates"] == [
        {"key": PROJECT_MONTHLY_ASK_APPROVED_STATE_KEY, "action": "set", "value": 40.0},
    ]


def test_over_monthly_budget_denies_any_model() -> None:
    """Over the hard monthly limit → DENY for any model (default hard stop).

    The default is a true hard stop — all models are blocked (a
    *downgrade* gate, not a session-terminating block: see the module
    docstring). The reason must surface the spend, say all calls are
    blocked, and frame it as the project's MONTHLY budget.
    """
    policy = project_monthly_cost_budget(max_cost_usd=50.0, ask_thresholds_usd=[20.0])
    for model in ("databricks-claude-opus-4-8", "databricks-claude-sonnet-4-6"):
        result = policy(_tool(60.0, model=model))
        assert result["result"] == "DENY", f"expected DENY for {model}"
        assert "60.00" in result["reason"]
        assert "All model calls are blocked" in result["reason"]
        assert "monthly" in result["reason"].lower()


def test_ask_message_names_the_project_scope() -> None:
    """The ASK reason is phrased as the project's spend (not a user's)."""
    policy = project_monthly_cost_budget(max_cost_usd=50.0, ask_thresholds_usd=[20.0])
    result = policy(_tool(20.0, project_id="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"))
    assert result["result"] == "ASK"
    assert result["reason"].startswith("This project's spend this month $20.00 passed")


def test_deny_message_names_the_project_scope() -> None:
    """The over-limit DENY reason also frames the spend as the project's."""
    policy = project_monthly_cost_budget(max_cost_usd=50.0, ask_thresholds_usd=[20.0])
    result = policy(_tool(60.0, model="databricks-claude-opus-4-8"))
    assert result["result"] == "DENY"
    assert "This project's spend $60.00 reached" in result["reason"]


def test_over_monthly_budget_on_cheaper_model_allows_with_explicit_list() -> None:
    """Over the monthly limit on a cheaper model → ALLOW when using an explicit expensive list.

    With explicit expensive_models (downgrade-gate mode), Sonnet is not in
    the list, so a downgraded session proceeds even over the monthly limit.
    """
    policy = project_monthly_cost_budget(max_cost_usd=50.0, expensive_models=["opus"])
    assert policy(_tool(60.0, model="databricks-claude-sonnet-4-6")) == {"result": "ALLOW"}


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"max_cost_usd": 0.0},
        {"max_cost_usd": 50.0, "ask_thresholds_usd": [50.0]},  # threshold == limit
        {"max_cost_usd": 50.0, "ask_thresholds_usd": [0.0]},  # threshold not > 0
        {"max_cost_usd": 50.0, "expensive_models": [""]},  # empty model token
    ],
)
def test_invalid_config_raises(bad_kwargs: dict[str, Any]) -> None:
    """Same validation as user_daily_cost_budget — bad config fails loud at build."""
    with pytest.raises(ValueError):
        project_monthly_cost_budget(**bad_kwargs)


def test_declined_monthly_checkpoint_reasks_until_approved() -> None:
    """An un-approved monthly checkpoint re-asks on every tool call.

    The monthly policy reads the approved highwater from the injected
    monthly rollup (``ask_approved_usd``), not session_state. With it at
    0.0, the same over-threshold monthly spend must ASK every time — a
    decline never records the approval (the engine withholds an ASK's
    state_updates on decline), so the gate keeps prompting until
    approved. Both calls must ASK.
    """
    policy = project_monthly_cost_budget(max_cost_usd=50.0, ask_thresholds_usd=[20.0])
    first = policy(_tool(30.0, ask_approved=0.0))
    second = policy(_tool(30.0, ask_approved=0.0))
    assert first["result"] == "ASK"
    assert second["result"] == "ASK"  # not recorded → re-asks


def test_over_monthly_budget_unknown_model_denies_fail_closed() -> None:
    """Over the monthly limit with an undeterminable model → DENY (fail closed)."""
    policy = project_monthly_cost_budget(max_cost_usd=50.0)
    result = policy(_tool(60.0, model=None))
    assert result["result"] == "DENY"


def test_monthly_deny_reason_for_codex_points_to_terminal() -> None:
    """The monthly DENY reason is harness-aware: codex-native → terminal /model."""
    policy = project_monthly_cost_budget(max_cost_usd=50.0, expensive_models=["opus"])
    result = policy(_tool(60.0, model="opus", harness="codex-native"))
    assert result["result"] == "DENY"
    assert "in the terminal" in result["reason"]
    assert "/model" in result["reason"]


def test_over_owner_raised_limit_clears_immediately() -> None:
    """A fresh call built with a higher max_cost_usd is no longer DENY.

    Mirrors the real recovery path: the owner raises `budget_config.limit_usd`,
    the next `build_policy_engine` call synthesizes the policy with the new
    limit (see `_load_project_budget_policy_specs`), and this same $60 spend
    is re-evaluated against it — with no separate "retry" mechanism needed.
    """
    over_limit = project_monthly_cost_budget(max_cost_usd=50.0)
    assert over_limit(_tool(60.0))["result"] == "DENY"
    raised_limit = project_monthly_cost_budget(max_cost_usd=100.0)
    assert raised_limit(_tool(60.0)) == {"result": "ALLOW"}
