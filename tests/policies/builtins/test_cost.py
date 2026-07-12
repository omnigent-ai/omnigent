"""
Tests for the built-in cost-budget policy
(:mod:`omnigent.policies.builtins.cost`) — the ``cost_budget`` factory.

The policy's hard limit gates both the ``request`` and ``tool_call``
phases: once reached, DENY (the whole turn on ``request``, or each tool
call on ``tool_call``) while the session is still on an expensive model
(forcing a ``/model`` downgrade), ALLOW once it has switched to a cheaper
one. The soft warning checkpoints ASK on BOTH the ``request`` and
``tool_call`` phases (each has a server-side approval round-trip that
persists the crossed checkpoint on accept).

Layers:

- **Layer 1** — direct callable on the ``request`` / ``tool_call``
  phases: ALLOW below the soft checkpoints, ASK (recorded via
  ``session_state`` so an approved checkpoint doesn't re-prompt) when one
  is crossed, DENY over the hard limit on an expensive/unknown model,
  ALLOW over the limit on a cheaper model, abstain on every non-gated
  phase, and factory validation.
- **Layer 2** — spec resolution through :func:`resolve_function_policy`,
  proving DENY and ASK thread through the engine boundary with the cost
  on ``EvaluationContext.usage`` and the active model on
  ``EvaluationContext.model``.
- **Layer 3** — registry discovery: the one ``POLICY_REGISTRY`` factory
  entry is browsable and its schema validates good / bad params.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.policies.builtins.cost import _ASK_APPROVED_KEY, cost_budget
from omnigent.policies.function import FunctionPolicy, resolve_function_policy
from omnigent.policies.registry import get_registry, load_registry, validate_factory_params
from omnigent.policies.schema import PolicyEvent
from omnigent.policies.types import EvaluationContext
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase, PolicyAction

_HANDLER = "omnigent.policies.builtins.cost.cost_budget"


def _tool(
    cost: float | None,
    *,
    model: str | None = "databricks-claude-opus-4-8",
    session_state: dict[str, Any] | None = None,
    harness: str | None = None,
) -> PolicyEvent:
    """
    Build a ``tool_call`` :class:`PolicyEvent` with a cost + active model.

    :param cost: ``total_cost_usd`` to place under ``context.usage``,
        e.g. ``2.5``. ``None`` omits the field entirely (the
        unpriced-session case).
    :param model: Active model under ``context.model``, e.g.
        ``"databricks-claude-opus-4-8"`` or the tier alias ``"opus"``.
        Defaults to an expensive (Opus) model; pass ``None`` for the
        undeterminable-model case.
    :param session_state: Optional persisted state, e.g.
        ``{_ASK_APPROVED_KEY: 2.0}``. ``None`` means empty.
    :param harness: Harness under ``context.harness``, e.g.
        ``"codex-native"`` (a native hook stamped it). ``None`` is the
        web / API / unstamped case, where the deny message stays
        surface-agnostic.
    :returns: A ``tool_call`` event dict.
    """
    usage: dict[str, Any] = {} if cost is None else {"total_cost_usd": cost}
    return {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {}},
        "context": {"actor": {}, "usage": usage, "model": model, "harness": harness},
        "session_state": session_state or {},
    }


def _request(
    cost: float | None,
    *,
    model: str | None = "databricks-claude-opus-4-8",
    session_state: dict[str, Any] | None = None,
    harness: str | None = None,
) -> PolicyEvent:
    """
    Build a ``request`` :class:`PolicyEvent` with a cost + active model.

    The request phase fires before the LLM turn; its ``data`` is the user
    message string and there is no tool ``target``. Used to prove the
    budget now gates whole turns (including text-only ones), not just
    tool calls.

    :param cost: ``total_cost_usd`` under ``context.usage``, e.g. ``6.0``.
        ``None`` omits the field (unpriced-session case).
    :param model: Active model under ``context.model``, e.g.
        ``"databricks-claude-opus-4-8"`` or the alias ``"opus"``; ``None``
        for the undeterminable-model case.
    :param session_state: Optional persisted state, e.g.
        ``{_ASK_APPROVED_KEY: 2.0}``. ``None`` means empty.
    :param harness: Harness under ``context.harness``; ``None`` is the
        web / API path (the request phase is not natively stamped).
    :returns: A ``request`` event dict.
    """
    usage: dict[str, Any] = {} if cost is None else {"total_cost_usd": cost}
    return {
        "type": "request",
        "target": None,
        "data": "please run the build",
        "context": {"actor": {}, "usage": usage, "model": model, "harness": harness},
        "session_state": session_state or {},
    }


def _event(phase: str, cost: float) -> PolicyEvent:
    """
    Build a non-gated-phase :class:`PolicyEvent` carrying a session cost.

    :param phase: Event type, e.g. ``"response"`` / ``"tool_result"`` /
        ``"llm_request"`` / ``"llm_response"`` (NOT ``"request"`` or
        ``"tool_call"``, which are gated).
    :param cost: ``total_cost_usd`` under ``context.usage``, e.g. ``9.99``.
    :returns: An event dict of the given phase (over budget, to prove the
        non-gated phases are not gated).
    """
    return {
        "type": phase,
        "target": None,
        "data": "x",
        "context": {"actor": {}, "usage": {"total_cost_usd": cost}, "model": "opus"},
        "session_state": {},
    }


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — direct callable
# ══════════════════════════════════════════════════════════════════════════════


def test_below_ask_threshold_allows() -> None:
    """Spend under the lowest checkpoint abstains (ALLOW)."""
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    assert policy(_tool(1.0)) == {"result": "ALLOW"}


def test_crossing_a_checkpoint_asks_and_records_it() -> None:
    """Crossing a checkpoint (unapproved) → ASK + record the crossed value.

    The ASK must carry a ``state_updates`` SET recording the crossed
    checkpoint so it (and lower ones) don't re-prompt once approved. A
    missing ``state_updates`` would mean the user is asked on every
    subsequent tool call even after approving.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0, 4.0])
    result = policy(_tool(2.0))  # exactly at the first checkpoint — `>=`
    assert result["result"] == "ASK"
    # SET highwater = 2.0: applied on approve so $2 (and lower) stop prompting.
    assert result["state_updates"] == [
        {"key": _ASK_APPROVED_KEY, "action": "set", "value": 2.0},
    ]


def test_approved_checkpoint_does_not_reprompt_higher_one_does() -> None:
    """Approved $2 → a $3 tool call is silent; reaching $4 ASKs again.

    Proves the "ASK at several amounts, once each on approve" behavior:
    the recorded highwater suppresses lower checkpoints, the next higher
    checkpoint still fires.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0, 4.0])
    # Already approved past $2 → a $3 tool call is allowed (no re-prompt).
    assert policy(_tool(3.0, session_state={_ASK_APPROVED_KEY: 2.0})) == {"result": "ALLOW"}
    # Crossing the next checkpoint ($4) prompts again.
    result = policy(_tool(4.0, session_state={_ASK_APPROVED_KEY: 2.0}))
    assert result["result"] == "ASK"
    assert result["state_updates"] == [
        {"key": _ASK_APPROVED_KEY, "action": "set", "value": 4.0},
    ]


def test_declined_checkpoint_reasks_until_approved() -> None:
    """A checkpoint not yet recorded re-asks on every tool call.

    A decline never writes the highwater (the engine withholds an ASK's
    ``state_updates`` on decline), so the next tool call still over the
    same threshold must ASK again — the gate keeps blocking until the
    user approves, not just once. Calling the policy twice with the same
    un-recorded state must ASK both times.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    first = policy(_tool(3.0, session_state={}))
    second = policy(_tool(3.0, session_state={}))
    assert first["result"] == "ASK"
    assert second["result"] == "ASK"  # not recorded → re-asks


def test_over_budget_denies_all_models_by_default() -> None:
    """Over the hard limit → DENY for any model when expensive_models is unset.

    The default (None) is a true hard stop: every model is blocked once
    the limit is reached, not just named expensive tiers. The reason must
    surface the spend figure and say all model calls are blocked.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    for model in ("databricks-claude-opus-4-8", "claude-sonnet-4-6", "gpt-5-mini", "haiku"):
        result = policy(_tool(6.0, model=model))
        assert result["result"] == "DENY", f"expected DENY for {model}"
        assert "6.00" in result["reason"]
        assert "All model calls are blocked" in result["reason"]


def test_deny_reason_for_codex_points_to_terminal() -> None:
    """A codex-native session's deny reason says to switch in the terminal.

    Codex has no web model picker — the only way to switch is the terminal
    TUI's ``/model`` — so the verbatim instruction must name both. Uses an
    explicit expensive_models list (downgrade-gate mode) so a cheaper model
    is possible and the switch hint applies.
    """
    policy = cost_budget(max_cost_usd=5.0, expensive_models=["opus"])
    result = policy(_tool(6.0, model="opus", harness="codex-native"))
    assert result["result"] == "DENY"
    assert "in the terminal" in result["reason"]
    assert "/model" in result["reason"]


def test_deny_reason_for_non_codex_omits_terminal() -> None:
    """A non-codex (or unstamped) session's deny reason stays surface-agnostic.

    Claude / web / API sessions are not terminal-only (they have a model
    picker), so the message must NOT tell them to use the terminal or
    ``/model`` — it would be wrong/confusing. Uses explicit expensive_models
    (downgrade-gate mode) so the switch hint is present but terminal-agnostic.
    """
    policy = cost_budget(max_cost_usd=5.0, expensive_models=["opus"])
    # harness=None mirrors the web/API path (no native hook stamped it).
    result = policy(_tool(6.0, model="opus", harness=None))
    assert result["result"] == "DENY"
    assert "in the terminal" not in result["reason"]
    assert "/model" not in result["reason"]
    assert "switch to a cheaper model" in result["reason"]


def test_over_budget_on_cheaper_model_allows_with_explicit_list() -> None:
    """Over the hard limit on a cheaper model → ALLOW when using an explicit expensive list.

    With explicit expensive_models (downgrade-gate mode), once the session
    has switched off a named expensive model, the budget becomes a no-op.
    A DENY here would trap the user even after they complied.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0], expensive_models=["opus"])
    assert policy(_tool(6.0, model="claude-sonnet-4-6")) == {"result": "ALLOW"}


def test_over_budget_unknown_model_denies_fail_closed() -> None:
    """Over the hard limit with no determinable model → DENY (fail closed).

    When the engine could not resolve a model (``None``), the gate cannot
    confirm a cheaper model, so it blocks and asks the user to pick one
    with ``/model`` rather than silently allowing unbounded spend. ALLOW
    here would let an over-budget session run unchecked whenever the model
    is unknown.
    """
    policy = cost_budget(max_cost_usd=5.0)
    assert policy(_tool(6.0, model=None))["result"] == "DENY"


def _unpriced_tool(input_tokens: int = 1000, model: str = "unpriced-model") -> PolicyEvent:
    """Build a ``tool_call`` event with token usage but no ``total_cost_usd``.

    Simulates a session that has consumed tokens on a model absent from the
    pricing catalog: ``total_cost_usd`` is never written, so the key is absent
    from the usage dict even though real spend has occurred.

    :param input_tokens: Cumulative input tokens to place in the usage dict.
    :param model: Active model id, e.g. ``"unpriced-model"``.
    :returns: A ``tool_call`` event with tokens but no cost key.
    """
    return {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {}},
        "context": {
            "actor": {},
            "usage": {"input_tokens": input_tokens, "total_tokens": input_tokens},
            "model": model,
        },
        "session_state": {},
    }


def test_unpriced_session_asks_fail_closed() -> None:
    """Token usage without ``total_cost_usd`` → ASK (fail closed with user bypass).

    A model absent from the pricing catalog never writes ``total_cost_usd``
    to the session, so the gate would score the session at $0 and never
    fire. After the fix, the gate detects tokens-present / cost-absent and
    returns ASK rather than silently treating unknown spend as $0. The
    ASK reason must name the model-pricing gap. The state_updates must
    carry the unpriced-approved key so an approval is remembered.
    """
    from omnigent.policies.schema import SESSION_COST_UNPRICED_APPROVED_KEY

    policy = cost_budget(max_cost_usd=5.0)
    result = policy(_unpriced_tool())
    assert result["result"] == "ASK"
    assert "pricing" in result["reason"].lower()
    keys = [u["key"] for u in result["state_updates"]]
    assert SESSION_COST_UNPRICED_APPROVED_KEY in keys


def test_unpriced_session_asks_both_phases() -> None:
    """Unpriced fail-closed fires on both tool_call and request phases."""
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[1.0])
    unpriced_request: PolicyEvent = {
        "type": "request",
        "target": None,
        "data": "hello",
        "context": {
            "actor": {},
            "usage": {"input_tokens": 500, "total_tokens": 500},
            "model": "unpriced-model",
        },
        "session_state": {},
    }
    assert policy(_unpriced_tool())["result"] == "ASK"
    assert policy(unpriced_request)["result"] == "ASK"


def test_unpriced_session_allows_after_approval() -> None:
    """Once the user approves the unpriced-model ASK, subsequent turns ALLOW.

    The state_updates key is recorded in session_state on approval. The
    next gate evaluation sees it and skips the unpriced check so the
    turn proceeds without re-asking.
    """
    from omnigent.policies.schema import SESSION_COST_UNPRICED_APPROVED_KEY

    policy = cost_budget(max_cost_usd=5.0)
    approved_event = _unpriced_tool()
    approved_event["session_state"] = {SESSION_COST_UNPRICED_APPROVED_KEY: True}
    assert policy(approved_event)["result"] == "ALLOW"


def test_no_tokens_yet_allows_first_turn() -> None:
    """No tokens in session_usage → ALLOW (first turn, nothing unpriced yet).

    The unpriced check must not fire when the session is brand new (no
    prior turns). The gate can only detect unpriced spend after at least
    one turn has written tokens; the very first turn on any model must
    be allowed so the gate is not infinitely recursive.
    """
    policy = cost_budget(max_cost_usd=5.0)
    # cost=None + no token keys → brand-new session, nothing spent
    result = policy(_tool(None))
    assert result["result"] == "ALLOW"


def test_priced_zero_cost_allows() -> None:
    """``total_cost_usd = 0.0`` (explicitly priced at zero) → not an unpriced session.

    A free model that IS in the catalog and reports $0 should behave
    normally — the key is present, the cost is zero, the gate allows.
    Confusing this with the absent-key case would block free catalog models.
    """
    policy = cost_budget(max_cost_usd=5.0)
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {}},
        "context": {
            "actor": {},
            "usage": {
                "input_tokens": 1000,
                "total_cost_usd": 0.0,  # explicitly priced at $0 (free model)
            },
            "model": "free-model",
        },
        "session_state": {},
    }
    assert policy(event)["result"] == "ALLOW"


def test_hard_limit_wins_over_checkpoint_approval() -> None:
    """Over the hard limit on an expensive model → DENY even if approved.

    A prior checkpoint approval must not let an over-budget session keep
    calling tools on the costly model; the hard gate is checked before
    the soft checkpoints.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0, 4.0])
    result = policy(_tool(5.0, model="opus", session_state={_ASK_APPROVED_KEY: 4.0}))
    assert result["result"] == "DENY"


@pytest.mark.parametrize(
    "model",
    [
        "databricks-claude-opus-4-8",
        "opus",
        "gpt-5",
        "gpt-5.5",
        "databricks-gpt-5-5",
        "claude-fable-5",
        "fable",
        # Cheap variants that used to be exempted are now also blocked.
        "gpt-5-mini",
        "gpt-5-nano",
        "databricks-gpt-5-mini",
        "databricks-claude-haiku-4-5",
        "databricks-gemini-2-5-pro",
    ],
)
def test_default_blocks_every_model(model: str) -> None:
    """The default (expensive_models=None) blocks every model over budget.

    The default is now a true hard stop — no model tier is "cheap enough"
    to continue once the limit is reached. Every model id must DENY.
    """
    policy = cost_budget(max_cost_usd=5.0)
    assert policy(_tool(6.0, model=model))["result"] == "DENY"


def test_custom_expensive_models_substring_case_insensitive() -> None:
    """A custom token matches case-insensitively as a substring.

    Proves the author can override the default set; ``"foo"`` must match
    ``"x-FOO-bar"`` so authors don't have to spell full provider-prefixed
    ids, and a non-matching model is allowed over budget.
    """
    policy = cost_budget(max_cost_usd=5.0, expensive_models=["FoO"])
    assert policy(_tool(6.0, model="x-foo-bar"))["result"] == "DENY"
    assert policy(_tool(6.0, model="claude-sonnet-4-6")) == {"result": "ALLOW"}


def test_explicit_expensive_models_apply_no_mini_nano_exclusion() -> None:
    """An explicit ``expensive_models`` list is matched literally — no excludes.

    The ``-mini`` / ``-nano`` carve-out applies ONLY to the built-in
    default set. When the author spells the tokens themselves, the set is
    matched exactly: ``["gpt-5"]`` then blocks ``gpt-5-mini`` too. If the
    exclusion leaked into explicit lists, an author who deliberately
    listed a cheap-variant-inclusive token could not enforce it.
    """
    policy = cost_budget(max_cost_usd=5.0, expensive_models=["gpt-5"])
    assert policy(_tool(6.0, model="gpt-5-mini"))["result"] == "DENY"
    assert policy(_tool(6.0, model="gpt-5-nano"))["result"] == "DENY"


def test_empty_expensive_models_blocks_all_models() -> None:
    """``expensive_models=[]`` makes the hard cap a true hard stop for all models.

    Over budget on any model — cheap or expensive — must be hard-DENYed.
    The DENY reason must say "All model calls are blocked" (no switch hint).
    The soft ASK still fires below the limit.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0], expensive_models=[])
    # Over budget on Opus → DENY (all models blocked).
    result = policy(_tool(6.0, model="opus", session_state={_ASK_APPROVED_KEY: 2.0}))
    assert result["result"] == "DENY"
    assert "All model calls are blocked" in result["reason"]
    # Over budget on a cheap model → also DENY.
    result_cheap = policy(_tool(6.0, model="haiku"))
    assert result_cheap["result"] == "DENY"
    assert "All model calls are blocked" in result_cheap["reason"]
    # Soft checkpoint still asks below the limit.
    assert policy(_tool(2.0, model="opus"))["result"] == "ASK"


@pytest.mark.parametrize("phase", ["response", "tool_result", "llm_request", "llm_response"])
def test_abstains_on_non_gated_phases(phase: str) -> None:
    """Only ``request`` / ``tool_call`` are gated — other phases abstain.

    The cost gate runs at ``request`` (before the turn) and ``tool_call``
    (the PreToolUse hook); an over-budget event of any other phase must
    ALLOW so the policy does not block post-hoc results or per-round-trip
    LLM events. (``request`` is covered by its own gating tests below.)
    """
    policy = cost_budget(max_cost_usd=1.0, ask_thresholds_usd=[0.5])
    assert policy(_event(phase, 9.99)) == {"result": "ALLOW"}


def test_request_phase_over_budget_denies_any_model() -> None:
    """Over the hard limit DENYs at the request phase for any model (default hard stop).

    The request phase fires before the LLM turn, so a text-only turn is
    budgeted. With the default (all-models-blocked), both expensive and
    cheap model ids must be denied. The reason must be the USER-FACING
    variant (no tool-call directive) and must say all calls are blocked.
    """
    policy = cost_budget(max_cost_usd=5.0)
    for model in ("databricks-claude-opus-4-8", "claude-sonnet-4-6"):
        result = policy(_request(6.0, model=model))
        assert result["result"] == "DENY", f"expected DENY for {model}"
        assert "6.00" in result["reason"]
        assert "All model calls are blocked" in result["reason"]
        assert "re-issue the tool call" not in result["reason"]
        assert "Relay this to the user verbatim" not in result["reason"]


def test_request_phase_over_budget_on_cheaper_model_allows_with_explicit_list() -> None:
    """Over the hard limit on a cheaper model ALLOWs when using an explicit expensive list.

    With an explicit expensive_models list (downgrade-gate mode), once the
    session is off a named expensive model, an over-budget request must
    proceed. A DENY here would trap a downgraded user out of starting any
    new turn.
    """
    policy = cost_budget(max_cost_usd=5.0, expensive_models=["opus"])
    assert policy(_request(6.0, model="claude-sonnet-4-6")) == {"result": "ALLOW"}


def test_request_phase_soft_checkpoint_asks_and_records_it() -> None:
    """A crossed soft checkpoint ASKs at the request phase → ASK + record.

    The request phase has a server-side approval round-trip (the engine
    parks the whole turn before it reaches the model and applies the ASK's
    ``state_updates`` only on accept), so a newly-crossed checkpoint must
    ASK here exactly as it does on ``tool_call`` — warning text-only turns
    too. The ASK carries the same ``state_updates`` SET so an approved
    checkpoint (and lower ones) won't re-prompt.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    # $2 is over the soft checkpoint but under the $5 hard cap.
    result = policy(_request(2.0, model="opus"))
    assert result["result"] == "ASK"
    assert result["state_updates"] == [
        {"key": _ASK_APPROVED_KEY, "action": "set", "value": 2.0},
    ]


def test_request_phase_below_threshold_allows() -> None:
    """Spend under the lowest checkpoint abstains (ALLOW) at the request phase."""
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    assert policy(_request(1.0, model="opus")) == {"result": "ALLOW"}


def test_request_phase_approved_checkpoint_does_not_reask() -> None:
    """An already-approved checkpoint does NOT re-ASK at the request phase.

    Once the request-phase ASK is approved its crossed value is persisted
    to ``session_state``; the next request under the same checkpoint must
    ALLOW (not re-prompt). A regression here would wedge every subsequent
    turn on the approval card.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    event = _request(2.0, model="opus", session_state={_ASK_APPROVED_KEY: 2.0})
    assert policy(event) == {"result": "ALLOW"}


def test_request_approval_carries_over_to_tool_call() -> None:
    """A request-phase approval suppresses the first tool call's re-ASK.

    The request phase records the crossed checkpoint on approve, so the
    first tool call of the same over-threshold turn sees it already
    approved and ALLOWs — the user is warned once per checkpoint, not
    twice (once on the turn, once on its first tool call).
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    state = {_ASK_APPROVED_KEY: 2.0}
    assert policy(_tool(2.0, model="opus", session_state=state)) == {"result": "ALLOW"}


def test_no_usage_at_all_allows() -> None:
    """No ``total_cost_usd`` AND no token counters → ALLOW (first turn).

    When session_usage has no tokens yet the session is brand new — the
    first turn on any model must be allowed because there's nothing to
    price yet. The unpriced-model ASK only fires once tokens have been
    written (i.e. after the first turn completes).
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    assert policy(_tool(None, model="opus")) == {"result": "ALLOW"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # neither max_cost_usd nor ask_thresholds_usd
        {"max_cost_usd": 0.0},  # non-positive hard limit
        {"max_cost_usd": -1.0},  # negative hard limit
        {"max_cost_usd": 5.0, "ask_thresholds_usd": [5.0]},  # not strictly below max
        {"max_cost_usd": 5.0, "ask_thresholds_usd": [6.0]},  # above max
        {"max_cost_usd": 5.0, "ask_thresholds_usd": [0.0]},  # not positive
        {"max_cost_usd": 5.0, "ask_thresholds_usd": [1.0, 6.0]},  # one above max
        {"max_cost_usd": 5.0, "expensive_models": [""]},  # empty model token
        {"max_cost_usd": 5.0, "expensive_models": [123]},  # non-string token
    ],
)
def test_factory_rejects_invalid_config(kwargs: dict[str, Any]) -> None:
    """Bad config fails loud at factory time (ValueError), not silently.

    Neither limit nor thresholds, a non-positive limit, a checkpoint outside
    ``(0, max_cost_usd)``, or a non-string / empty ``expensive_models`` entry
    is a misconfiguration that could never enforce correctly, so it must raise
    rather than build a dead gate.
    """
    with pytest.raises(ValueError):
        cost_budget(**kwargs)


def test_ask_thresholds_only_no_hard_cap() -> None:
    """cost_budget with only ask_thresholds_usd never denies, only asks."""
    policy = cost_budget(ask_thresholds_usd=[1.0, 3.0])
    # Below threshold — allow.
    assert policy(_tool(0.5, model="opus")) == {"result": "ALLOW"}
    # Crossed threshold — ask.
    result = policy(_tool(2.0, model="opus"))
    assert result["result"] == "ASK"
    assert "1.00" in result["reason"]
    # No hard-cap message (no max_cost_usd in reason).
    assert "limit" not in result["reason"]
    # Way over threshold — still only asks, never denies.
    result = policy(_tool(100.0, model="opus"))
    assert result["result"] == "ASK"


# ══════════════════════════════════════════════════════════════════════════════
# Layer 2 — spec resolution through resolve_function_policy
# ══════════════════════════════════════════════════════════════════════════════


def _tool_ctx(cost: float, model: str | None) -> EvaluationContext:
    """
    Build a TOOL_CALL :class:`EvaluationContext` with cost + model set.

    Mirrors what the engine injects (``usage`` + ``model``) so a directly
    resolved policy sees the same ``event["context"]`` it would in
    production.

    :param cost: ``total_cost_usd`` for the usage context, e.g. ``6.0``.
    :param model: Active model id for ``ctx.model``, e.g. ``"opus"`` or
        ``None``.
    :returns: A ready-to-evaluate TOOL_CALL context.
    """
    return EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "sys_os_shell", "arguments": {}},
        tool_name="sys_os_shell",
        usage={"total_cost_usd": cost},
        model=model,
    )


@pytest.mark.asyncio
async def test_resolve_from_spec_denies_over_budget_on_expensive_model() -> None:
    """Over-budget on an expensive model DENYs through the engine boundary.

    Proves the cost on ``EvaluationContext.usage`` AND the model on
    ``EvaluationContext.model`` both reach the resolved callable (via
    ``event["context"]``) and the DENY threads back as a
    :class:`PolicyAction`.
    """
    spec = FunctionPolicySpec(
        name="cost",
        on=None,
        function=FunctionRef(path=_HANDLER, arguments={"max_cost_usd": 5.0}),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    result = await policy.evaluate(_tool_ctx(6.0, "databricks-claude-opus-4-8"), {})
    assert result.action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_resolve_from_spec_allows_over_budget_on_cheaper_model() -> None:
    """Over-budget on a cheaper model ALLOWs through the engine boundary.

    With an explicit expensive_models list (downgrade-gate mode), the model
    on ``EvaluationContext.model`` lets a downgraded session through —
    proving the model gate (not just the cost) crosses the boundary.
    """
    spec = FunctionPolicySpec(
        name="cost",
        on=None,
        function=FunctionRef(
            path=_HANDLER, arguments={"max_cost_usd": 5.0, "expensive_models": ["opus"]}
        ),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    result = await policy.evaluate(_tool_ctx(6.0, "claude-sonnet-4-6"), {})
    assert result.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_resolve_from_spec_asks_in_soft_zone() -> None:
    """Soft-zone spend surfaces as ASK through the engine boundary."""
    spec = FunctionPolicySpec(
        name="cost",
        on=None,
        function=FunctionRef(
            path=_HANDLER, arguments={"max_cost_usd": 5.0, "ask_thresholds_usd": [2.0]}
        ),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    result = await policy.evaluate(_tool_ctx(3.0, "opus"), {})
    assert result.action == PolicyAction.ASK


@pytest.mark.asyncio
async def test_resolve_from_spec_denies_over_budget_on_request_phase() -> None:
    """Over-budget on an expensive model DENYs at the REQUEST phase too.

    The request phase is the path :func:`_evaluate_request_policy` in the
    server uses (it builds a ``Phase.REQUEST`` ``EvaluationContext`` with
    ``usage`` + ``model``). This proves the cost + model thread through the
    engine boundary on that phase and the DENY comes back as a
    :class:`PolicyAction`, so a text-only over-budget turn is blocked
    before the LLM runs.
    """
    spec = FunctionPolicySpec(
        name="cost",
        on=None,
        function=FunctionRef(path=_HANDLER, arguments={"max_cost_usd": 5.0}),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    ctx = EvaluationContext(
        phase=Phase.REQUEST,
        content="please run the build",
        tool_name=None,
        usage={"total_cost_usd": 6.0},
        model="databricks-claude-opus-4-8",
    )
    result = await policy.evaluate(ctx, {})
    assert result.action == PolicyAction.DENY


# ══════════════════════════════════════════════════════════════════════════════
# Layer 3 — registry discovery
# ══════════════════════════════════════════════════════════════════════════════


def test_registry_discovers_cost_budget() -> None:
    """The cost_budget factory is browsable in the policy registry."""
    load_registry()
    by_handler = {e.handler: e for e in get_registry()}
    assert _HANDLER in by_handler
    assert by_handler[_HANDLER].kind == "factory"
    assert by_handler[_HANDLER].params_schema is not None


def test_registry_validates_factory_params() -> None:
    """The registry schema accepts good params and rejects bad ones."""
    load_registry()
    # Valid: hard limit alone, soft gate alone, both together, and with models.
    assert validate_factory_params(_HANDLER, {"max_cost_usd": 5.0}) is None
    assert validate_factory_params(_HANDLER, {"ask_thresholds_usd": [1.0]}) is None
    assert (
        validate_factory_params(_HANDLER, {"max_cost_usd": 5.0, "ask_thresholds_usd": [2.0]})
        is None
    )
    assert (
        validate_factory_params(_HANDLER, {"max_cost_usd": 5.0, "expensive_models": ["opus"]})
        is None
    )
    # Wrong type for the checkpoints (must be an array, not a scalar).
    assert (
        validate_factory_params(_HANDLER, {"max_cost_usd": 5.0, "ask_thresholds_usd": 2.0})
        is not None
    )
    # Wrong type for expensive_models (must be an array, not a scalar).
    assert (
        validate_factory_params(_HANDLER, {"max_cost_usd": 5.0, "expensive_models": "opus"})
        is not None
    )
    # Unknown param.
    err_unknown = validate_factory_params(_HANDLER, {"max_cost_usd": 5.0, "bogus": 1})
    assert err_unknown is not None and "bogus" in err_unknown
    # Wrong type for the hard limit.
    assert validate_factory_params(_HANDLER, {"max_cost_usd": "lots"}) is not None
