"""
Tests for the built-in cost-budget policy
(:mod:`omnigent.policies.builtins.cost`) — the ``cost_budget`` factory.

The policy's hard limit gates both the ``request`` and ``tool_call``
phases: once reached, DENY (the whole turn on ``request``, or each tool
call on ``tool_call``) while the session is still on an expensive model
(forcing a ``/model`` downgrade), ALLOW once it has switched to a cheaper
one. The soft warning checkpoints ASK on ``tool_call`` only (the
request-phase path has no approval round-trip).

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


def test_over_budget_on_expensive_model_denies() -> None:
    """Over the hard limit on an expensive model → DENY (force downgrade).

    The default expensive set includes Opus; an over-budget tool call on
    Opus must be blocked, and the reason must surface the spend figure and
    the high-cost model tokens so the user knows what to avoid. If this
    ALLOWed, the budget would never bite on the costly model it targets.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    result = policy(_tool(6.0, model="databricks-claude-opus-4-8"))
    assert result["result"] == "DENY"
    assert "6.00" in result["reason"]  # current cost surfaced
    # The high-cost tokens are listed so the user knows which to avoid.
    assert "opus" in result["reason"]
    assert "gpt-5" in result["reason"]


def test_deny_reason_for_codex_points_to_terminal() -> None:
    """A codex-native session's deny reason says to switch in the terminal.

    Codex has no web model picker — the only way to switch is the terminal
    TUI's ``/model`` — so the verbatim instruction must name both. If this
    regressed to the surface-agnostic wording, a codex user would not be
    told the one mechanism that actually works for them.
    """
    policy = cost_budget(max_cost_usd=5.0)
    result = policy(_tool(6.0, model="opus", harness="codex-native"))
    assert result["result"] == "DENY"
    assert "in the terminal" in result["reason"]
    assert "/model" in result["reason"]


def test_deny_reason_for_non_codex_omits_terminal() -> None:
    """A non-codex (or unstamped) session's deny reason stays surface-agnostic.

    Claude / web / API sessions are not terminal-only (they have a model
    picker), so the message must NOT tell them to use the terminal or
    ``/model`` — it would be wrong/confusing. This is the regression guard
    for "only codex says in the terminal".
    """
    policy = cost_budget(max_cost_usd=5.0)
    # harness=None mirrors the web/API path (no native hook stamped it).
    result = policy(_tool(6.0, model="opus", harness=None))
    assert result["result"] == "DENY"
    assert "in the terminal" not in result["reason"]
    assert "/model" not in result["reason"]
    assert "switch to a cheaper model" in result["reason"]


def test_over_budget_on_cheaper_model_allows() -> None:
    """Over the hard limit on a cheaper model → ALLOW (downgrade satisfied).

    Once the session has switched off an expensive model, the budget
    becomes a no-op — the whole point of a "downgrade gate" rather than a
    hard stop. A DENY here would trap the user even after they complied.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
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
    "model,expected",
    [
        # Opus — concrete deployment id and the bare picker alias.
        ("databricks-claude-opus-4-8", "DENY"),
        ("opus", "DENY"),
        # GPT-5 family: the broad "gpt-5" token covers bare gpt-5, the
        # dot-spelled 5.5, and the dash-spelled deployment id.
        ("gpt-5", "DENY"),
        ("gpt-5.5", "DENY"),
        ("databricks-gpt-5-5", "DENY"),
        # Fable is the costliest tier (above Opus at 2x its price); both the
        # concrete id and the bare picker alias must be gated, or switching to
        # Fable becomes a budget bypass for a session downgraded off Opus.
        ("claude-fable-5", "DENY"),
        ("fable", "DENY"),
        # Cheap GPT-5 variants are carved out by the -mini / -nano excludes,
        # even though they contain the "gpt-5" substring → ALLOW over budget.
        ("gpt-5-mini", "ALLOW"),
        ("gpt-5-nano", "ALLOW"),
        ("databricks-gpt-5-mini", "ALLOW"),
        # A non-listed model is treated as cheap → allowed over budget.
        ("databricks-claude-haiku-4-5", "ALLOW"),
        # "gemini" contains the substring "mini" but NOT "-mini"; the
        # leading dash in the exclude token keeps it from being wrongly
        # carved out (it's simply not an expensive token either) → ALLOW.
        ("databricks-gemini-2-5-pro", "ALLOW"),
    ],
)
def test_default_expensive_set_matches_and_excludes(model: str, expected: str) -> None:
    """The default set blocks Fable/Opus/GPT-5 but exempts -mini/-nano.

    Substring + case-insensitive matching must hit the deployment ids in
    this stack (``databricks-claude-opus-4-8``, ``databricks-gpt-5-5``)
    while the cheap ``gpt-5-mini`` / ``gpt-5-nano`` variants — which share
    the ``gpt-5`` substring — are carved back out so they are NOT blocked.
    A miss either way would mis-budget the zero-config default: blocking a
    cheap variant traps users needlessly; letting Opus/GPT-5 through lets
    the costliest models run past budget.
    """
    policy = cost_budget(max_cost_usd=5.0)
    assert policy(_tool(6.0, model=model))["result"] == expected


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


def test_empty_expensive_models_disables_hard_gate() -> None:
    """``expensive_models=[]`` disables the hard gate (soft thresholds only).

    Over budget on Opus must NOT be hard-DENYed (no model is blocked);
    with the soft checkpoint already approved it ALLOWs. The soft ASK
    still fires below the limit — proving the empty list scopes off only
    the hard block, not the whole policy.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0], expensive_models=[])
    # Over budget on Opus, checkpoint already approved → ALLOW (no hard DENY).
    assert policy(_tool(6.0, model="opus", session_state={_ASK_APPROVED_KEY: 2.0})) == {
        "result": "ALLOW"
    }
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


def test_request_phase_over_budget_on_expensive_model_denies() -> None:
    """Over the hard limit on an expensive model DENYs at the request phase.

    The request phase fires before the LLM turn, so a text-only turn (no
    tool call) is now budgeted: an over-budget request on Opus must be
    blocked. The reason must be the USER-FACING variant (the turn never
    reaches the model), so it must NOT carry the tool-call directive
    ("re-issue the tool call" / "Relay this to the user verbatim"). If
    this regressed, text-only turns would bypass the budget entirely, or
    the user would see model-directed instructions meant for the agent.
    """
    policy = cost_budget(max_cost_usd=5.0)
    result = policy(_request(6.0, model="databricks-claude-opus-4-8"))
    assert result["result"] == "DENY"
    assert "6.00" in result["reason"]  # current cost surfaced to the user
    # User-facing phrasing only — no tool-call directive leaks through.
    assert "re-issue the tool call" not in result["reason"]
    assert "Relay this to the user verbatim" not in result["reason"]
    # Still names the limit + how to recover so the user can act.
    assert "switch to a cheaper model" in result["reason"]


def test_request_phase_over_budget_on_cheaper_model_allows() -> None:
    """Over the hard limit on a cheaper model ALLOWs at the request phase.

    Mirrors the tool-call downgrade gate: once the session is off an
    expensive model, an over-budget request must proceed. A DENY here
    would trap a downgraded user out of starting any new turn.
    """
    policy = cost_budget(max_cost_usd=5.0)
    assert policy(_request(6.0, model="claude-sonnet-4-6")) == {"result": "ALLOW"}


def test_request_phase_soft_checkpoint_does_not_ask() -> None:
    """A crossed soft checkpoint does NOT ASK at the request phase → ALLOW.

    The soft gate is tool-call only: the request-phase (input) policy path
    surfaces an ASK as a plain denial with no approval round-trip and no
    ``state_updates`` persistence, so emitting "Continue?" there would
    just block the turn and re-prompt forever. Below the hard cap, an
    over-threshold request must therefore ALLOW (the warning fires at the
    first tool call instead). A regression to ASK here would wedge every
    text-only turn once spend passes the first checkpoint.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    # $2 is over the soft checkpoint but under the $5 hard cap.
    assert policy(_request(2.0, model="opus")) == {"result": "ALLOW"}


def test_request_phase_below_threshold_allows() -> None:
    """Spend under the lowest checkpoint abstains (ALLOW) at the request phase."""
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    assert policy(_request(1.0, model="opus")) == {"result": "ALLOW"}


def test_tool_call_still_asks_for_soft_checkpoint_after_request_allows() -> None:
    """The soft warning still fires — at the first tool call, not the request.

    Pairs with :func:`test_request_phase_soft_checkpoint_does_not_ask`:
    the same over-threshold spend that ALLOWs on ``request`` must ASK on
    ``tool_call`` (where approval + state persistence work). This proves
    the warning was relocated, not lost.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    result = policy(_tool(2.0, model="opus"))
    assert result["result"] == "ASK"
    assert result["state_updates"] == [
        {"key": _ASK_APPROVED_KEY, "action": "set", "value": 2.0},
    ]


def test_unpriced_session_never_trips() -> None:
    """No ``total_cost_usd`` (pricing unavailable) → ALLOW, never blocks.

    Defaults to ``0.0``; the policy cannot budget what it cannot price,
    so it must abstain rather than block every tool call at $0 — even on
    an expensive model.
    """
    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    assert policy(_tool(None, model="opus")) == {"result": "ALLOW"}


@pytest.mark.parametrize(
    "kwargs",
    [
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

    A non-positive limit, a checkpoint outside ``(0, max_cost_usd)``, or a
    non-string / empty ``expensive_models`` entry is a misconfiguration
    that could never enforce correctly, so it must raise rather than build
    a dead gate.
    """
    with pytest.raises(ValueError):
        cost_budget(**kwargs)


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

    The model on ``EvaluationContext.model`` must let a downgraded session
    through — proving the model gate (not just the cost) crosses the
    boundary.
    """
    spec = FunctionPolicySpec(
        name="cost",
        on=None,
        function=FunctionRef(path=_HANDLER, arguments={"max_cost_usd": 5.0}),
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
    # Valid: required hard limit alone, with the soft gate, and with models.
    assert validate_factory_params(_HANDLER, {"max_cost_usd": 5.0}) is None
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
    # Missing the required hard limit.
    assert validate_factory_params(_HANDLER, {}) is not None
    # Unknown param.
    err_unknown = validate_factory_params(_HANDLER, {"max_cost_usd": 5.0, "bogus": 1})
    assert err_unknown is not None and "bogus" in err_unknown
    # Wrong type for the hard limit.
    assert validate_factory_params(_HANDLER, {"max_cost_usd": "lots"}) is not None
