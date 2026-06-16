"""Tests for the per-user daily cost-budget policy.

``user_daily_cost_budget`` gates the ``request`` and ``tool_call`` phases
on the session OWNER's cumulative spend for the current UTC day, read from
``event["context"]["user_daily_cost"]`` (injected by the engine). Same
ASK/DENY/downgrade logic as ``cost_budget``, but:

- the budget is the owner's daily spend, not the session's;
- an approved soft checkpoint is read from / recorded to the daily
  store (``ask_approved_usd``) rather than ``session_state``, via the
  reserved ``USER_DAILY_ASK_APPROVED_STATE_KEY`` state-update the engine
  routes to that store.

These exercise the policy callable directly with synthetic events — the
engine wiring (injection + state-update routing) is covered by the
engine and store tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.policies.builtins.cost import user_daily_cost_budget
from omnigent.policies.schema import USER_DAILY_ASK_APPROVED_STATE_KEY, PolicyEvent


def _tool(
    cost: float | None,
    *,
    ask_approved: float = 0.0,
    model: str | None = "databricks-claude-opus-4-8",
    harness: str | None = None,
    owner: str | None = None,
) -> PolicyEvent:
    """
    Build a ``tool_call`` event carrying the owner's daily cost rollup.

    :param cost: ``cost_usd`` under ``context.user_daily_cost``, e.g.
        ``2.5``. ``None`` omits the field (unpriced / no-owner case).
    :param ask_approved: ``ask_approved_usd`` under
        ``context.user_daily_cost`` — the highest checkpoint the owner
        already approved today, e.g. ``2.0``.
    :param model: Active model under ``context.model``; defaults to an
        expensive (Opus) model. ``None`` is the undeterminable case.
    :param harness: ``context.harness``, e.g. ``"codex-native"``;
        ``None`` is the web / API / unstamped case.
    :param owner: ``user_id`` under ``context.user_daily_cost`` — the
        session owner the rollup belongs to, e.g. ``"alice@example.com"``.
        ``None`` omits it (single-user mode), exercising the un-named
        message fallback.
    :returns: A ``tool_call`` event dict.
    """
    daily: dict[str, Any] = (
        {} if cost is None else {"cost_usd": cost, "ask_approved_usd": ask_approved}
    )
    if owner is not None:
        daily["user_id"] = owner
    return {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {}},
        "context": {"actor": {}, "user_daily_cost": daily, "model": model, "harness": harness},
        "session_state": {},
    }


def test_below_ask_threshold_allows() -> None:
    """Daily spend under the lowest checkpoint abstains (ALLOW)."""
    policy = user_daily_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    assert policy(_tool(1.0)) == {"result": "ALLOW"}


@pytest.mark.parametrize("phase", ["response", "tool_result", "llm_request", "llm_response"])
def test_non_gated_phase_allows(phase: str) -> None:
    """Non-gated phases abstain even over budget (request/tool_call ARE gated)."""
    policy = user_daily_cost_budget(max_cost_usd=5.0)
    event: PolicyEvent = {
        "type": phase,
        "target": None,
        "data": "x",
        "context": {"user_daily_cost": {"cost_usd": 9.99}, "model": "opus"},
        "session_state": {},
    }
    assert policy(event) == {"result": "ALLOW"}


def test_request_phase_over_budget_on_expensive_model_denies() -> None:
    """Over the hard daily limit on an expensive model DENYs at the request phase.

    The daily gate now also fires before the LLM turn, so a text-only turn
    counts against the daily budget. The reason must be the user-facing
    variant (no tool-call directive), since a request-phase DENY is shown
    straight to the user.
    """
    policy = user_daily_cost_budget(max_cost_usd=5.0)
    event: PolicyEvent = {
        "type": "request",
        "target": None,
        "data": "please run the build",
        "context": {
            "actor": {},
            "user_daily_cost": {"cost_usd": 6.0, "ask_approved_usd": 0.0},
            "model": "databricks-claude-opus-4-8",
        },
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "DENY"
    assert "6.00" in result["reason"]
    assert "daily" in result["reason"].lower()
    # User-facing phrasing only — no tool-call directive leaks through.
    assert "re-issue the tool call" not in result["reason"]


def test_request_phase_soft_checkpoint_asks_and_records_daily_key() -> None:
    """Crossing a daily checkpoint ASKs at the request phase → ASK + daily key.

    The soft warning now also fires before the LLM turn (the request phase
    has a server-side approval round-trip), so a text-only turn that
    crosses a daily checkpoint parks for approval. The ASK still records to
    the per-user+day store via ``USER_DAILY_ASK_APPROVED_STATE_KEY``.
    """
    policy = user_daily_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    event: PolicyEvent = {
        "type": "request",
        "target": None,
        "data": "please run the build",
        "context": {
            "actor": {},
            "user_daily_cost": {"cost_usd": 2.0, "ask_approved_usd": 0.0},
            "model": "databricks-claude-opus-4-8",
        },
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "ASK"
    assert result["state_updates"] == [
        {"key": USER_DAILY_ASK_APPROVED_STATE_KEY, "action": "set", "value": 2.0},
    ]


def test_zero_or_missing_daily_cost_allows() -> None:
    """No daily cost recorded (no owner / unpriced) → never trips."""
    policy = user_daily_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[1.0])
    # cost=None omits the field entirely → reads as 0.0.
    assert policy(_tool(None)) == {"result": "ALLOW"}


def test_crossing_a_checkpoint_asks_and_records_daily_key() -> None:
    """Crossing a daily checkpoint (unapproved) → ASK + reserved daily state key.

    The ASK must carry a ``state_updates`` SET on
    ``USER_DAILY_ASK_APPROVED_STATE_KEY`` (NOT the session key) so the
    engine routes the approval to the per-user+day store — that's what
    makes an approval persist across the user's other sessions today. A
    missing/ wrong key would re-prompt every session.
    """
    policy = user_daily_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0, 4.0])
    result = policy(_tool(2.0))  # exactly at the first checkpoint — `>=`
    assert result["result"] == "ASK"
    assert result["state_updates"] == [
        {"key": USER_DAILY_ASK_APPROVED_STATE_KEY, "action": "set", "value": 2.0},
    ]


def test_approved_checkpoint_from_daily_store_does_not_reprompt() -> None:
    """Approval read from context.user_daily_cost.ask_approved_usd suppresses re-ask.

    Unlike the session policy (which reads session_state), the daily
    policy reads the approved highwater from the injected daily rollup.
    With ask_approved=2.0, a $3 daily-spend tool call is silent; reaching
    the next checkpoint ($4) ASKs again.
    """
    policy = user_daily_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0, 4.0])
    # Already approved past $2 today (from a possibly different session) → silent.
    assert policy(_tool(3.0, ask_approved=2.0)) == {"result": "ALLOW"}
    # Crossing the next checkpoint prompts again.
    result = policy(_tool(4.0, ask_approved=2.0))
    assert result["result"] == "ASK"
    assert result["state_updates"] == [
        {"key": USER_DAILY_ASK_APPROVED_STATE_KEY, "action": "set", "value": 4.0},
    ]


def test_over_daily_budget_on_expensive_model_denies() -> None:
    """Over the hard daily limit on an expensive model → DENY (force downgrade).

    The reason must surface the spend and the high-cost tokens, and frame
    it as the DAILY budget (not the session one) so the message is
    accurate.
    """
    policy = user_daily_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    result = policy(_tool(6.0, model="databricks-claude-opus-4-8"))
    assert result["result"] == "DENY"
    assert "6.00" in result["reason"]
    assert "opus" in result["reason"]
    # Framed as the per-user daily budget, not the session one.
    assert "daily" in result["reason"].lower()


def test_ask_message_names_the_owner_when_present() -> None:
    """The ASK reason names whose daily spend tripped the gate.

    When the engine injects ``user_daily_cost.user_id``, the warning reads
    "<owner>'s spend today …" so a shared-deployment user knows the budget
    is theirs (not the session's), which is the whole point of the per-user
    daily gate.
    """
    policy = user_daily_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    result = policy(_tool(2.0, owner="alice@example.com"))
    assert result["result"] == "ASK"
    assert result["reason"].startswith("alice@example.com's spend today $2.00 passed")


def test_ask_message_falls_back_to_unnamed_without_owner() -> None:
    """No injected owner (single-user mode) → the original un-named phrasing."""
    policy = user_daily_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    result = policy(_tool(2.0))  # no owner
    assert result["result"] == "ASK"
    assert result["reason"].startswith("Today's spend $2.00 passed")
    assert "'s spend today" not in result["reason"]


def test_deny_message_names_the_owner_when_present() -> None:
    """The over-limit DENY reason also names the owner ("<owner>'s spend …")."""
    policy = user_daily_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    result = policy(_tool(6.0, model="databricks-claude-opus-4-8", owner="bob@example.com"))
    assert result["result"] == "DENY"
    assert "bob@example.com's spend $6.00 reached" in result["reason"]


def test_over_daily_budget_on_cheaper_model_allows() -> None:
    """Over the daily limit but already on a cheaper model → ALLOW (downgrade satisfied).

    Sonnet is outside the default expensive set, so a downgraded session
    proceeds even over the daily limit. (Note: ``gpt-5-4`` is NOT a cheap
    model under the broad ``gpt-5`` default token — only the ``-mini`` /
    ``-nano`` variants are carved out.)
    """
    policy = user_daily_cost_budget(max_cost_usd=5.0)
    assert policy(_tool(6.0, model="databricks-claude-sonnet-4-6")) == {"result": "ALLOW"}


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"max_cost_usd": 0.0},
        {"max_cost_usd": 5.0, "ask_thresholds_usd": [5.0]},  # threshold == limit
        {"max_cost_usd": 5.0, "ask_thresholds_usd": [0.0]},  # threshold not > 0
        {"max_cost_usd": 5.0, "expensive_models": [""]},  # empty model token
    ],
)
def test_invalid_config_raises(bad_kwargs: dict[str, Any]) -> None:
    """Same validation as cost_budget — bad config fails loud at build."""
    with pytest.raises(ValueError):
        user_daily_cost_budget(**bad_kwargs)


def test_declined_daily_checkpoint_reasks_until_approved() -> None:
    """An un-approved daily checkpoint re-asks on every tool call.

    The daily policy reads the approved highwater from the injected
    daily rollup (``ask_approved_usd``), not session_state. With it at
    0.0, the same over-threshold daily spend must ASK every time — a
    decline never records the approval (the engine withholds an ASK's
    state_updates on decline), so the gate keeps prompting until
    approved. Both calls must ASK.
    """
    policy = user_daily_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    first = policy(_tool(3.0, ask_approved=0.0))
    second = policy(_tool(3.0, ask_approved=0.0))
    assert first["result"] == "ASK"
    assert second["result"] == "ASK"  # not recorded → re-asks


def test_over_daily_budget_unknown_model_denies_fail_closed() -> None:
    """Over the daily limit with an undeterminable model → DENY (fail closed).

    A ``None`` model must not silently allow unbounded spend; the gate
    blocks and asks the user to pick a (knowable, cheaper) model.
    """
    policy = user_daily_cost_budget(max_cost_usd=5.0)
    result = policy(_tool(6.0, model=None))
    assert result["result"] == "DENY"


def test_daily_deny_reason_for_codex_points_to_terminal() -> None:
    """The daily DENY reason is harness-aware: codex-native → terminal /model.

    Guards that the per-user daily factory wires the harness through to
    the (shared) deny-reason builder, so a codex user is told the one
    switch mechanism that works for them.
    """
    policy = user_daily_cost_budget(max_cost_usd=5.0)
    result = policy(_tool(6.0, model="opus", harness="codex-native"))
    assert result["result"] == "DENY"
    assert "in the terminal" in result["reason"]
    assert "/model" in result["reason"]
