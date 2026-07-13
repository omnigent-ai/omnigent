"""Built-in cost-budget policy.

A single factory, :func:`cost_budget`, that gates a session on its
cumulative LLM spend (USD) at the **request** phase (before the LLM
turn, so text-only turns are budgeted too) and the **tool-call** phase
(the point a native ``PreToolUse`` hook can block before the action
runs):

- ``ask_thresholds_usd`` (optional, soft, **request + tool-call
  phases**): a list of warning checkpoints. The first time the session's
  ``total_cost_usd`` crosses each checkpoint, the turn (request phase) or
  the tool call (tool-call phase) is parked for user approval (ASK). Each
  checkpoint prompts at most once *per approval* — the highest-approved
  checkpoint is remembered in ``session_state``, so approving lets spend
  continue to the next checkpoint. A decline blocks that one turn / tool
  call but does not record the checkpoint, so the next request or tool
  call over the same threshold re-asks until it is approved. (Both phases
  have a server-side approval round-trip that applies the ASK's
  ``state_updates`` only on accept — the request phase parks the whole
  turn before it reaches the model, so text-only turns are warned too.)
- ``max_cost_usd`` (required, hard, **request + tool-call phases**): once
  spend reaches this, the policy forces a model downgrade. Rather than
  stopping the session, it DENYs **while the session is still on an
  expensive model** (``expensive_models``) — the whole turn at the
  request phase, or each tool call — telling the user to switch to a
  cheaper model with ``/model``. Once the session has switched off an
  expensive model it is allowed again — the budget becomes a "downgrade
  gate," not a hard stop.

It reads cumulative spend from
``event["context"]["usage"]["total_cost_usd"]`` — the running session
total maintained server-side (token-priced for relay/codex sessions,
billed directly for claude-native) — and the active model from
``event["context"]["model"]`` (the conversation's ``model_override`` or
the agent spec's ``llm.model``, resolved by the policy engine). When a
model has no catalog pricing, ``total_cost_usd`` is never written to the
session, so the policy would score the session at ``$0`` and never
enforce the budget. To prevent unpriced spend silently bypassing the cap,
the gate **fails closed** when token usage is present but
``total_cost_usd`` is absent: it returns DENY with a message asking the
user to switch to a priced model.

On the ``tool_call`` phase a DENY/ASK blocks that specific tool call
(the native hook returns ``deny`` / parks for approval) rather than
ending the session. On the ``request`` phase a DENY/ASK blocks the whole
turn before the model runs — so text-only turns with no tool calls are
budgeted (DENY) and warned (ASK) too; the verdict is surfaced straight
to the user via the same server-side approval round-trip. Cost is
refreshed at turn boundaries, so a single very expensive turn can still
overshoot before the next check.

YAML usage::

    policies:
      cost_budget:
        type: function
        function:
          path: omnigent.policies.builtins.cost.cost_budget
          arguments:
            max_cost_usd: 5.0
            ask_thresholds_usd: [1.0, 2.5]
            expensive_models: ["opus", "gpt-5"]

The factory must be referenced via ``function: {path, arguments}`` with
a non-empty ``arguments`` block (the registry declares it
``kind: "factory"``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.policies.schema import (
    SESSION_COST_ASK_APPROVED_STATE_KEY,
    SESSION_COST_UNPRICED_APPROVED_KEY,
    USER_DAILY_ASK_APPROVED_STATE_KEY,
    PolicyCallable,
    PolicyEvent,
    PolicyResponse,
    StateUpdateEntry,
)

_logger = logging.getLogger(__name__)

_ALLOW: PolicyResponse = {"result": "ALLOW"}

# session_state key recording that the user has acknowledged and approved
# continuing a session whose active model has no catalog pricing. Routed to
# the ROOT conversation (like SESSION_COST_ASK_APPROVED_STATE_KEY) so a
# single approval on the parent covers the whole spawn tree.
_UNPRICED_APPROVED_KEY = SESSION_COST_UNPRICED_APPROVED_KEY

# ASK response emitted when the session has consumed tokens on a model with
# no catalog pricing and the user has not yet acknowledged it. The gate
# cannot enforce a budget it cannot measure — asking rather than denying lets
# the operator or user make an informed choice, while still preventing silent
# pass-through at $0.
_UNPRICED_ASK: PolicyResponse = {
    "result": "ASK",
    "reason": (
        "The active model has no catalog pricing so cumulative spend cannot be "
        "tracked. Continuing will allow untracked spend that counts against "
        "neither the cap nor the warning thresholds. Switch to a priced model "
        "to restore budget enforcement, or approve to continue without it."
    ),
    "state_updates": [
        {
            "key": _UNPRICED_APPROVED_KEY,
            "action": "set",
            "value": True,
        }
    ],
}


def _usage_is_unpriced(usage: dict[str, Any]) -> bool:
    """Return ``True`` when token usage is present but cost is unpriced.

    The session has had at least one turn (token counters are non-zero) yet
    ``total_cost_usd`` is absent — meaning no turn was ever priced by the
    catalog. The gate cannot enforce a budget against this session: it would
    score every check at ``$0`` and never fire. Callers use this to fail
    closed (DENY) rather than fail open.

    Returns ``False`` when ``total_cost_usd`` is already present (priced) or
    when no tokens have been consumed yet (first request, nothing to price).
    The first turn on an unpriced model still runs — the gate only has
    post-turn data at check time — but every subsequent turn is denied.

    :param usage: ``event["context"]["usage"]`` or equivalent subtree dict.
    :returns: ``True`` when enforcement should fail closed due to missing
        pricing.
    """
    if "total_cost_usd" in usage:
        return False
    return bool(
        usage.get("input_tokens") or usage.get("output_tokens") or usage.get("total_tokens")
    )


# Phases the budget gate fires on. ``tool_call`` is the native ``PreToolUse``
# block point; ``request`` runs before the LLM turn so text-only turns (no
# tool call) are budgeted too. Every other phase abstains (ALLOW).
_GATED_PHASES = frozenset({"request", "tool_call"})

# session_state key recording the highest ``ask_thresholds_usd`` checkpoint
# the user has already approved continuing past (a USD float; 0.0 when none).
# Set when an ASK is approved so each checkpoint prompts at most once. Shared
# with the engine, which routes it to the ROOT conversation so the approval
# covers the whole spawn tree (the budget is per-session, but a sub-agent runs
# as its own conversation).
_ASK_APPROVED_KEY = SESSION_COST_ASK_APPROVED_STATE_KEY


def _session_cost_usd(event: PolicyEvent) -> float:
    """Read cumulative session cost (USD) from a policy event.

    :param event: Policy event dict.
    :returns: ``event["context"]["usage"]["total_cost_usd"]`` as a
        float, or ``0.0`` when the field is absent / not yet priced.
    """
    context = event.get("context") or {}
    usage = context.get("usage") or {}
    raw = usage.get("total_cost_usd", 0.0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        # Defensive: a malformed usage payload must not crash the gate.
        return 0.0


def _current_model(event: PolicyEvent) -> str | None:
    """Read the session's active model from a policy event.

    :param event: Policy event dict.
    :returns: ``event["context"]["model"]`` as a string, e.g.
        ``"databricks-claude-opus-4-8"`` or the tier alias ``"opus"``;
        ``None`` when the engine could not determine a model (no
        ``model_override`` and no spec ``llm``).
    """
    context = event.get("context") or {}
    model = context.get("model")
    return model if isinstance(model, str) and model else None


def _current_harness(event: PolicyEvent) -> str | None:
    """Read the harness name from a policy event, if one was stamped.

    Native tool hooks (e.g. the codex ``PreToolUse`` hook) stamp the harness
    into the event context so the deny message can be tailored to how that
    harness lets the user switch model. Web / API / unstamped paths leave it
    absent.

    :param event: Policy event dict.
    :returns: ``event["context"]["harness"]`` (e.g. ``"codex-native"``), or
        ``None`` when not stamped.
    """
    context = event.get("context") or {}
    harness = context.get("harness")
    return harness if isinstance(harness, str) and harness else None


def _over_budget_deny_reason(
    cost: float,
    max_cost_usd: float,
    expensive_tokens: tuple[str, ...],
    harness: str | None,
    *,
    phase: str = "tool_call",
    policy_label: str = "session cost-budget",
    budget_label: str = "cost budget",
    subject_user: str | None = None,
    block_all: bool = False,
) -> str:
    """Build the over-budget DENY reason for the budget gate.

    On the ``request`` phase the DENY reason is surfaced directly to the
    user (the turn never reaches the model), so it is the plain
    user-facing message — no "relay this verbatim" / "re-issue the tool
    call" wrapper. On the ``tool_call`` phase the reason is handed to the
    model by native harnesses, so it is phrased as a DIRECTIVE (see
    below).

    Phrased as a DIRECTIVE to the agent (not a statement): native harnesses
    hand this reason to the model, which otherwise paraphrases it and drops
    the actionable instruction — so it is told to relay the quoted message
    verbatim and wait. Crucially it also says the block is NOT permanent and
    that a user request to retry (after switching model) means *actually
    re-issue the tool call*: without this, the model treats an earlier "do not
    retry" as standing and, when the user later asks again, just repeats the
    cached message instead of re-running the tool (so the gate never
    re-evaluates against the now-cheaper model). The quoted message (a) names
    the high-cost model tiers so the user knows what to avoid, and (b) tailors
    the switch instruction to the harness: codex-native users can only change
    model from the terminal TUI (no web picker), so they are pointed there;
    every other surface (claude web picker, API, …) gets a surface-agnostic
    instruction.

    :param cost: Current cumulative session spend in USD, e.g. ``6.0``.
    :param max_cost_usd: The hard limit in USD, e.g. ``5.0``.
    :param expensive_tokens: The high-cost model substring tokens, listed for
        the user, e.g. ``("opus", "gpt-5")``.
    :param harness: The harness name from the event (see
        :func:`_current_harness`), e.g. ``"codex-native"``; ``None`` when
        unstamped.
    :param phase: The enforcement phase the DENY is for — ``"request"``
        (user-facing message) or ``"tool_call"`` (model-directed directive).
        Defaults to ``"tool_call"``.
    :param policy_label: Which budget policy is speaking, woven into
        "Blocked by the {policy_label} policy". Defaults to
        ``"session cost-budget"``; the per-user daily variant passes
        ``"per-user daily cost-budget"``.
    :param budget_label: The budget noun in the user-facing line,
        defaults to ``"cost budget"``; the daily variant passes
        ``"daily cost budget"``.
    :param subject_user: When given (the per-user daily variant), names
        whose spend tripped the gate — rendered as ``"<user>'s spend"``.
        ``None`` (the session variant) keeps the un-named ``"spend"`` so
        the session cost-budget output is unchanged.
    :returns: The DENY reason string.
    """
    spend_subject = f"{subject_user}'s spend" if subject_user else "spend"
    if block_all:
        # All models are blocked (expensive_models=[]): no cheaper model exists
        # to switch to, so the message is a hard stop with no switch hint.
        verbatim = (
            f"You've hit the ${max_cost_usd:.2f} {budget_label}. "
            f"All model calls are blocked over budget."
        )
        if phase == "request":
            return (
                f"Blocked by the {policy_label} policy: {spend_subject} ${cost:.2f} reached the "
                f"${max_cost_usd:.2f} limit. {verbatim}"
            )
        return (
            f"Blocked by the {policy_label} policy: {spend_subject} ${cost:.2f} reached the "
            f"${max_cost_usd:.2f} limit, and all further tool calls are blocked. Relay this to "
            f"the user verbatim, then stop and wait for them — do not silently re-run the tool "
            f'right now: "{verbatim}"'
        )
    expensive_list = ", ".join(expensive_tokens) or "the configured high-cost models"
    if harness is not None and "codex" in harness:
        switch_hint = "in the terminal, run /model and pick a cheaper model to continue"
    else:
        switch_hint = "switch to a cheaper model to continue"
    verbatim = (
        f"You've hit the ${max_cost_usd:.2f} {budget_label}. High-cost models "
        f"({expensive_list}) are blocked over budget — {switch_hint}."
    )
    if phase == "request":
        # Request-phase DENY: surfaced straight to the user (the turn never
        # reaches the model), so this is the plain message — no relay wrapper.
        return (
            f"Blocked by the {policy_label} policy: {spend_subject} ${cost:.2f} reached the "
            f"${max_cost_usd:.2f} limit. {verbatim}"
        )
    return (
        f"Blocked by the {policy_label} policy: {spend_subject} ${cost:.2f} reached the "
        f"${max_cost_usd:.2f} limit, and tool calls are blocked while on a high-cost "
        f"model. Relay this to the user verbatim, then stop and wait for them — do not "
        f'silently re-run the tool right now: "{verbatim}" This block is NOT permanent: '
        f"once the user switches to a cheaper model and asks you to continue, actually "
        f"re-issue the tool call (it will be allowed) — do not just repeat this message."
    )


def _model_blocked_over_budget(
    model: str | None,
    expensive_tokens: tuple[str, ...],
    exclude_tokens: tuple[str, ...] = (),
    *,
    block_all: bool = False,
) -> bool:
    """Decide whether the active model is blocked once over budget.

    Returns ``True`` when the session must downgrade to keep calling
    tools — i.e. the model matches one of the expensive tokens (and is
    not carved out by an exclude token), OR the model is undeterminable
    (``None``). Failing closed on an unknown model keeps the budget
    enforceable: rather than silently allowing unbounded spend, it asks
    the user to pick a cheaper model with ``/model`` (which sets
    ``model_override``, making the model knowable and — if cheap —
    unblocking the session).

    An exclude token takes precedence over an expensive token: a model
    matching both (e.g. ``gpt-5-mini`` matches ``"gpt-5"`` and the
    exclude ``"-mini"``) is NOT blocked. This lets a broad expensive
    token (``"gpt-5"``) cover a whole family while exempting its cheap
    variants.

    When ``block_all=True`` (``expensive_models=[]`` was passed to the
    factory) every model is blocked — the budget is a true hard stop.

    :param model: The active model id, or ``None`` when
        undeterminable.
    :param expensive_tokens: Lowercased substring tokens identifying
        expensive models, e.g. ``("opus", "gpt-5")``.
    :param exclude_tokens: Lowercased substring tokens that override an
        expensive match back to "not expensive", e.g.
        ``("-mini", "-nano")``. Defaults to empty (no exclusions).
    :param block_all: When ``True``, return ``True`` for every model
        (``expensive_models=[]`` semantics — all models blocked).
    :returns: ``True`` when tool calls should be DENYed over budget.
    """
    if block_all:
        return True
    if model is None:
        return True
    low = model.lower()
    if any(token in low for token in exclude_tokens):
        return False
    return any(token in low for token in expensive_tokens)


@dataclass(frozen=True)
class _ExpensiveModelConfig:
    """Resolved expensive-model matching configuration for a budget factory.

    :param expensive_tokens: Lowercased substring tokens that mark a
        model as expensive, e.g. ``("fable", "opus", "gpt-5")``.
    :param exclude_tokens: Lowercased substring tokens that override an
        expensive match back to "not expensive", e.g. ``("-mini",
        "-nano")``. Non-empty only for the built-in default set; empty
        when the caller supplies an explicit ``expensive_models`` list.
    :param hard_cap_enabled: Whether the hard over-budget DENY gate is
        active. Always ``True`` — including when the caller passes an
        empty ``expensive_models`` list, which means *all* models are
        blocked once over budget (see ``block_all_models``).
    :param block_all_models: When ``True`` (set only when the caller
        passes ``expensive_models=[]``) every model — not just a named
        expensive tier — is blocked once the hard limit is reached.
        ``_model_blocked_over_budget`` short-circuits to ``True``
        regardless of the active model id.
    """

    expensive_tokens: tuple[str, ...]
    exclude_tokens: tuple[str, ...]
    hard_cap_enabled: bool
    block_all_models: bool = False


def _resolve_expensive_models(expensive_models: list[str] | None) -> _ExpensiveModelConfig:
    """Resolve the ``expensive_models`` factory argument into matching config.

    Shared by :func:`cost_budget` and :func:`user_daily_cost_budget` so
    both treat the argument identically:

    - ``None`` or ``[]`` → **all models blocked** once over budget
      (``block_all_models=True``, hard gate on). This is a true hard
      stop: no model can continue once the limit is reached.
    - a non-empty list → those tokens (lowercased), matched literally
      with no exclusions (the caller controls the set exactly); hard gate
      on (downgrade gate — cheaper models still allowed over budget).

    :param expensive_models: The factory argument, e.g.
        ``["opus", "gpt-5"]``, ``None``, or ``[]``.
    :returns: The resolved :class:`_ExpensiveModelConfig`.
    :raises ValueError: If any entry is not a non-empty string.
    """
    if expensive_models is None or len(expensive_models) == 0:
        return _ExpensiveModelConfig(
            expensive_tokens=(),
            exclude_tokens=(),
            hard_cap_enabled=True,
            block_all_models=True,
        )
    for m in expensive_models:
        if not isinstance(m, str) or not m:
            raise ValueError(f"each expensive_models value must be a non-empty string, got {m!r}")
    expensive_tokens = tuple(m.lower() for m in expensive_models)
    return _ExpensiveModelConfig(
        expensive_tokens=expensive_tokens,
        exclude_tokens=(),
        hard_cap_enabled=True,
    )


def cost_budget(
    max_cost_usd: float | None = None,
    ask_thresholds_usd: list[float] | None = None,
    expensive_models: list[str] | None = None,
) -> PolicyCallable:
    """Factory: gate a session on cumulative LLM spend (USD).

    The hard limit (when set) gates BOTH the ``request`` phase (blocking
    the whole turn before the LLM runs, so text-only turns are budgeted
    too) and the ``tool_call`` phase: once the limit is reached, DENY
    while the session is still on an expensive model — telling the user
    to ``/model`` to a cheaper one. The soft warning checkpoints (ASK
    "continue?"; recorded on approve so they don't re-prompt, re-asked
    after a decline) fire on BOTH the ``request`` and ``tool_call``
    phases — both have a server-side approval round-trip (see
    ``evaluate``). Abstains (ALLOW) on every other phase and whenever
    cost is unpriced (``0.0``).

    :param max_cost_usd: Optional hard limit in USD. Once cumulative
        session cost reaches this, tool calls are DENYed while the
        session is on an expensive model, e.g. ``5.0``. Must be ``> 0``
        if provided. Either this or *ask_thresholds_usd* must be set.
    :param ask_thresholds_usd: Optional soft warning checkpoints in USD,
        e.g. ``[1.0, 2.5]``. Each ASKs for approval the first time
        cumulative cost crosses it (approval remembered via
        ``session_state``, so an approved checkpoint prompts at most
        once; a decline blocks the one turn / tool call and re-asks next
        time). ``None`` or ``[]`` disables the soft gate. Every value must be
        ``> 0`` and strictly less than *max_cost_usd* when both are set.
        Order does not matter — they are sorted internally.
    :param expensive_models: Optional case-insensitive substring tokens
        identifying the model tiers blocked once over *max_cost_usd*,
        e.g. ``["opus", "gpt-5"]``. A token matches when it is a
        substring of the active model id (so ``"opus"`` matches both
        ``"databricks-claude-opus-4-8"`` and the alias ``"opus"``).
        ``None`` (the default) or ``[]`` makes the hard cap a true hard
        stop: **all** models are blocked once the limit is reached. Pass
        an explicit non-empty list for a downgrade gate that only blocks
        the named tiers, letting cheaper models continue over budget.
        Each value must be a non-empty string.
    :returns: A policy callable implementing the budget gate.
    :raises ValueError: If neither *max_cost_usd* nor *ask_thresholds_usd*
        is provided, *max_cost_usd* is not positive, any
        *ask_thresholds_usd* value is not in ``(0, max_cost_usd)`` when
        both are set, or any *expensive_models* entry is not a non-empty
        string.
    """
    if max_cost_usd is None and not ask_thresholds_usd:
        raise ValueError("cost_budget requires max_cost_usd and/or ask_thresholds_usd")
    if max_cost_usd is not None and max_cost_usd <= 0:
        raise ValueError(f"max_cost_usd must be > 0, got {max_cost_usd!r}")
    thresholds = sorted({float(t) for t in (ask_thresholds_usd or [])})
    for t in thresholds:
        if max_cost_usd is not None and not (0 < t < max_cost_usd):
            raise ValueError(
                f"each ask_thresholds_usd value must be in "
                f"(0, max_cost_usd={max_cost_usd}), got {t!r}"
            )
    cfg = _resolve_expensive_models(expensive_models)

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        """Evaluate the session cost budget for a request or tool call.

        Gates the ``request`` phase (before the LLM turn, so text-only
        turns are budgeted too) and the ``tool_call`` phase (the native
        ``PreToolUse`` block) — abstains on every other phase.

        - ``cost >= max_cost_usd`` and the active model is expensive
          (or undeterminable) → DENY (switch to a cheaper model);
          ``cost >= max_cost_usd`` on a cheaper model → ALLOW. This hard
          gate runs on BOTH gated phases.
        - the highest soft checkpoint newly crossed and not yet
          approved → ASK ("continue?") carrying a ``state_updates``
          write of the crossed value, applied only on approve so the
          checkpoint (and lower ones) won't re-prompt once approved.
          This soft gate runs on BOTH gated phases: each has a
          server-side approval round-trip that parks the turn (request)
          or tool call (tool_call) and persists the ASK's
          ``state_updates`` only on accept. Firing on ``request`` means
          text-only turns are warned too, and the recorded checkpoint
          stops the first tool call of the same turn from re-asking.

        :param event: Policy event dict.
        :returns: DENY when over budget on an expensive model; ASK when
            a new soft checkpoint is newly crossed; ALLOW otherwise.
        """
        phase = event.get("type")
        if phase not in _GATED_PHASES:
            return _ALLOW
        context = event.get("context") or {}
        if _usage_is_unpriced(context.get("usage") or {}):
            if not (event.get("session_state") or {}).get(_UNPRICED_APPROVED_KEY):
                return _UNPRICED_ASK
            return _ALLOW
        cost = _session_cost_usd(event)
        if max_cost_usd is not None and cfg.hard_cap_enabled and cost >= max_cost_usd:
            if _model_blocked_over_budget(
                _current_model(event),
                cfg.expensive_tokens,
                cfg.exclude_tokens,
                block_all=cfg.block_all_models,
            ):
                return {
                    "result": "DENY",
                    "reason": _over_budget_deny_reason(
                        cost,
                        max_cost_usd,
                        cfg.expensive_tokens,
                        _current_harness(event),
                        phase=phase,
                        block_all=cfg.block_all_models,
                    ),
                }
            # Already on a cheaper model — the downgrade gate is satisfied.
            return _ALLOW
        if thresholds:
            # Highest checkpoint the cost has crossed so far.
            crossed = max((t for t in thresholds if cost >= t), default=None)
            if crossed is not None:
                state = event.get("session_state") or {}
                approved_up_to = float(state.get(_ASK_APPROVED_KEY, 0.0) or 0.0)
                if crossed > approved_up_to:
                    limit_str = f" (limit ${max_cost_usd:.2f})" if max_cost_usd is not None else ""
                    return {
                        "result": "ASK",
                        "reason": (
                            f"Session cost ${cost:.2f} passed the ${crossed:.2f} "
                            f"warning threshold{limit_str}. Continue?"
                        ),
                        # Applied only on approve → this and every lower
                        # checkpoint won't re-prompt; higher ones still will.
                        # A declined ASK leaves this unset, so the next
                        # request or tool call over the same threshold re-asks.
                        "state_updates": [
                            {"key": _ASK_APPROVED_KEY, "action": "set", "value": crossed},
                        ],
                    }
        return _ALLOW

    return evaluate  # type: ignore[return-value]


def _user_daily_cost_usd(event: PolicyEvent) -> float:
    """Read the session owner's per-UTC-day cost (USD) from a policy event.

    :param event: Policy event dict.
    :returns: ``event["context"]["user_daily_cost"]["cost_usd"]`` as a
        float, or ``0.0`` when absent (engine didn't inject it — e.g. no
        owner / not priced), so the gate never trips on missing data.
    """
    context = event.get("context") or {}
    daily = context.get("user_daily_cost") or {}
    raw = daily.get("cost_usd", 0.0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _user_daily_ask_approved_usd(event: PolicyEvent) -> float:
    """Read the highest soft checkpoint the owner approved today (USD).

    :param event: Policy event dict.
    :returns: ``event["context"]["user_daily_cost"]["ask_approved_usd"]``
        as a float, or ``0.0`` when absent / none approved yet.
    """
    context = event.get("context") or {}
    daily = context.get("user_daily_cost") or {}
    raw = daily.get("ask_approved_usd", 0.0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _user_daily_owner(event: PolicyEvent) -> str | None:
    """Read the session owner the daily rollup belongs to, from a policy event.

    Used to name whose spend tripped the gate in the ASK / DENY message.

    :param event: Policy event dict.
    :returns: ``event["context"]["user_daily_cost"]["user_id"]`` as a
        non-empty string, or ``None`` when absent (single-user mode / not
        injected) — callers fall back to an un-named phrasing.
    """
    context = event.get("context") or {}
    daily = context.get("user_daily_cost") or {}
    owner = daily.get("user_id")
    return owner if isinstance(owner, str) and owner else None


def user_daily_cost_budget(
    max_cost_usd: float,
    ask_thresholds_usd: list[float] | None = None,
    expensive_models: list[str] | None = None,
) -> PolicyCallable:
    """Factory: gate on the session OWNER's per-UTC-day LLM spend (USD).

    Identical gating logic to :func:`cost_budget`, but the budget is the
    session owner's **cumulative spend across all their sessions today
    (UTC)** instead of this one session's spend. It reads
    ``event["context"]["user_daily_cost"]`` (``cost_usd`` /
    ``ask_approved_usd``), which the policy engine injects — at
    engine-build time — only when this policy is configured (from the
    ``user_daily_cost`` store, attributed to the session owner). The hard
    limit and the soft warning checkpoints both gate the ``request`` phase
    (before the LLM turn) and the ``tool_call`` phase (see
    :func:`cost_budget`).

    - **Soft (`ask_thresholds_usd`)**: the first time the owner's daily
      spend crosses a checkpoint, the turn (request phase) or tool call
      (tool-call phase) is parked for approval (ASK). The approval is
      recorded **per user+day** (in ``user_daily_cost.ask_approved_usd``
      via a reserved ``state_updates`` key the engine routes to that
      store), so an approved checkpoint won't re-prompt the user again
      that day — including from a different session. A decline blocks
      that one turn / tool call and re-asks next time.
    - **Hard (`max_cost_usd`)**: once the owner's daily spend reaches
      the limit, DENY every tool call while the session is on an
      ``expensive_models`` model (a ``/model`` downgrade gate, not a
      stop); ALLOW once on a cheaper model.

    Abstains (ALLOW) on every other phase, and whenever the daily cost
    is ``0.0`` (no spend recorded, no owner, or pricing unavailable).

    :param max_cost_usd: Hard daily limit in USD. Must be ``> 0``.
    :param ask_thresholds_usd: Optional soft daily warning checkpoints
        in USD, e.g. ``[1.0, 2.5]``. Each value must be ``> 0`` and
        ``< max_cost_usd``. ``None`` / ``[]`` disables the soft gate.
    :param expensive_models: Optional case-insensitive substring tokens
        for the model tiers blocked once over the daily limit. ``None``
        (the default) or ``[]`` makes the hard cap a true hard stop —
        all models are blocked once the limit is reached. Pass an
        explicit non-empty list for a downgrade gate that only blocks the
        named tiers.
    :returns: A policy callable implementing the per-user daily budget.
    :raises ValueError: Same validation as :func:`cost_budget`.
    """
    if max_cost_usd <= 0:
        raise ValueError(f"max_cost_usd must be > 0, got {max_cost_usd!r}")
    thresholds = sorted({float(t) for t in (ask_thresholds_usd or [])})
    for t in thresholds:
        if not (0 < t < max_cost_usd):
            raise ValueError(
                f"each ask_thresholds_usd value must be in "
                f"(0, max_cost_usd={max_cost_usd}), got {t!r}"
            )
    cfg = _resolve_expensive_models(expensive_models)

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        """Evaluate the per-user daily cost budget for a request or tool call.

        Mirrors :func:`cost_budget`'s ``evaluate`` exactly, reading the
        owner's daily spend / approval instead of the session totals,
        and recording an approved checkpoint to the user+day store
        (reserved ``state_updates`` key) rather than ``session_state``.

        :param event: Policy event dict.
        :returns: DENY when over the daily budget on an expensive model;
            ASK when a new daily soft checkpoint is newly crossed; ALLOW
            otherwise.
        """
        phase = event.get("type")
        if phase not in _GATED_PHASES:
            return _ALLOW
        context = event.get("context") or {}
        if _usage_is_unpriced(context.get("usage") or {}):
            if not (event.get("session_state") or {}).get(_UNPRICED_APPROVED_KEY):
                return _UNPRICED_ASK
            return _ALLOW
        cost = _user_daily_cost_usd(event)
        owner = _user_daily_owner(event)
        if cfg.hard_cap_enabled and cost >= max_cost_usd:
            if _model_blocked_over_budget(
                _current_model(event),
                cfg.expensive_tokens,
                cfg.exclude_tokens,
                block_all=cfg.block_all_models,
            ):
                return {
                    "result": "DENY",
                    "reason": _over_budget_deny_reason(
                        cost,
                        max_cost_usd,
                        cfg.expensive_tokens,
                        _current_harness(event),
                        phase=phase,
                        policy_label="per-user daily cost-budget",
                        budget_label="daily cost budget",
                        subject_user=owner,
                        block_all=cfg.block_all_models,
                    ),
                }
            return _ALLOW
        # Soft ASK fires on both gated phases — each has a server-side
        # approval round-trip that persists the checkpoint on accept (see
        # cost_budget.evaluate).
        if thresholds:
            crossed = max((t for t in thresholds if cost >= t), default=None)
            if crossed is not None:
                approved_up_to = _user_daily_ask_approved_usd(event)
                if crossed > approved_up_to:
                    spend_subject = f"{owner}'s spend today" if owner else "Today's spend"
                    return {
                        "result": "ASK",
                        "reason": (
                            f"{spend_subject} ${cost:.2f} passed the ${crossed:.2f} "
                            f"daily warning threshold (daily limit ${max_cost_usd:.2f}). "
                            f"Continue?"
                        ),
                        # Reserved key — the engine routes this to
                        # user_daily_cost.ask_approved_usd (per user+day),
                        # applied only on approve, so it won't re-prompt
                        # today across the user's sessions.
                        "state_updates": [
                            {
                                "key": USER_DAILY_ASK_APPROVED_STATE_KEY,
                                "action": "set",
                                "value": crossed,
                            },
                        ],
                    }
        return _ALLOW

    return evaluate  # type: ignore[return-value]


# session_state key recording the highest ``ask_thresholds_usd`` checkpoint
# the user has already approved continuing past for a SUBAGENT cost budget.
# Unlike ``_ASK_APPROVED_KEY`` (which routes to the ROOT conversation), this
# stays local to the child's own session_state so approvals are scoped to the
# subagent, not the whole spawn tree.
_SUBAGENT_ASK_APPROVED_KEY = "subagent_cost_ask_approved_usd"


def _subtree_cost_usd(event: PolicyEvent) -> float:
    """Read cumulative subtree cost (USD) from a policy event.

    :param event: Policy event dict.
    :returns: ``event["context"]["subtree_usage"]["total_cost_usd"]`` as a
        float, or ``0.0`` when the field is absent / not yet priced.
    """
    context = event.get("context") or {}
    subtree_usage = context.get("subtree_usage") or {}
    raw = subtree_usage.get("total_cost_usd", 0.0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def subagent_cost_budget(
    max_cost_usd: float | None = None,
    ask_thresholds_usd: list[float] | None = None,
    expensive_models: list[str] | None = None,
) -> PolicyCallable:
    """Factory: gate a sub-agent on its own subtree LLM spend (USD).

    Identical gating logic to :func:`cost_budget`, but scoped to the
    **child conversation's subtree** (itself + its descendants) rather
    than the whole session tree. Reads
    ``event["context"]["subtree_usage"]["total_cost_usd"]`` instead of
    ``event["context"]["usage"]["total_cost_usd"]``.

    Intended to be attached to a child session at spawn time via
    ``sys_session_send``'s ``cost_budget`` argument. The parent sets the
    budget; the child gates against its own subtree spend.

    The soft-checkpoint approval key (``subagent_cost_ask_approved_usd``)
    stays local to the child's ``session_state`` — it is NOT routed to
    the root conversation, so approvals are scoped to the subagent.

    :param max_cost_usd: Optional hard limit in USD for the subtree. Must be
        ``> 0`` if provided. Either this or ask_thresholds_usd must be set.
    :param ask_thresholds_usd: Optional soft warning checkpoints in USD.
        Same semantics as :func:`cost_budget`.
    :param expensive_models: Optional case-insensitive substring tokens.
        Same semantics as :func:`cost_budget`.
    :returns: A policy callable implementing the subtree budget gate.
    :raises ValueError: If neither max_cost_usd nor ask_thresholds_usd is set,
        or if validation fails.
    """
    # At least one of max_cost_usd or ask_thresholds_usd must be present.
    if max_cost_usd is None and not ask_thresholds_usd:
        raise ValueError("subagent_cost_budget requires max_cost_usd and/or ask_thresholds_usd")
    if max_cost_usd is not None and max_cost_usd <= 0:
        raise ValueError(f"max_cost_usd must be > 0, got {max_cost_usd!r}")
    thresholds = sorted({float(t) for t in (ask_thresholds_usd or [])})
    for t in thresholds:
        if max_cost_usd is not None and not (0 < t < max_cost_usd):
            raise ValueError(
                f"each ask_thresholds_usd value must be in "
                f"(0, max_cost_usd={max_cost_usd}), got {t!r}"
            )
    cfg = _resolve_expensive_models(expensive_models)

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        """Evaluate the subagent subtree cost budget for a request or tool call.

        Same gating logic as :func:`cost_budget`'s ``evaluate``, reading
        the subtree cost and using a local approval key.

        :param event: Policy event dict.
        :returns: DENY when over budget on an expensive model; ASK when
            a new soft checkpoint is newly crossed; ALLOW otherwise.
        """
        phase = event.get("type")
        if phase not in _GATED_PHASES:
            return _ALLOW
        context = event.get("context") or {}
        if _usage_is_unpriced(context.get("subtree_usage") or {}):
            if not (event.get("session_state") or {}).get(_UNPRICED_APPROVED_KEY):
                return _UNPRICED_ASK
            return _ALLOW
        cost = _subtree_cost_usd(event)
        # Check hard limit if max_cost_usd is set.
        if max_cost_usd is not None and cfg.hard_cap_enabled and cost >= max_cost_usd:
            if _model_blocked_over_budget(
                _current_model(event),
                cfg.expensive_tokens,
                cfg.exclude_tokens,
                block_all=cfg.block_all_models,
            ):
                return {
                    "result": "DENY",
                    "reason": _over_budget_deny_reason(
                        cost,
                        max_cost_usd,
                        cfg.expensive_tokens,
                        _current_harness(event),
                        phase=phase,
                        policy_label="subagent cost-budget",
                        budget_label="subagent cost budget",
                        block_all=cfg.block_all_models,
                    ),
                }
            return _ALLOW
        # Check soft thresholds if ask_thresholds_usd is set.
        if thresholds:
            crossed = max((t for t in thresholds if cost >= t), default=None)
            if crossed is not None:
                state = event.get("session_state") or {}
                approved_up_to = float(state.get(_SUBAGENT_ASK_APPROVED_KEY, 0.0) or 0.0)
                if crossed > approved_up_to:
                    limit_str = f" (limit ${max_cost_usd:.2f})" if max_cost_usd else ""
                    return {
                        "result": "ASK",
                        "reason": (
                            f"Subagent subtree cost ${cost:.2f} passed the ${crossed:.2f} "
                            f"warning threshold{limit_str}. Continue?"
                        ),
                        "state_updates": [
                            {
                                "key": _SUBAGENT_ASK_APPROVED_KEY,
                                "action": "set",
                                "value": crossed,
                            },
                        ],
                    }
        return _ALLOW

    return evaluate  # type: ignore[return-value]


# ── Real spend budget (AB#2899) ──────────────────────────────────────────
#
# Unlike cost_budget / user_daily_cost_budget / subagent_cost_budget above
# (which gate on LLM token spend tracked in-process), this factory gates on
# REAL business spend tracked OUTSIDE the engine entirely, by an external
# "goettl-core Harness Status" oracle HTTP service. The oracle owns both the
# running spend totals (spend_today / spend_mtd) AND the four operator-set
# thresholds (daily ask/limit, MTD ask/limit) — an operator can tighten or
# loosen the budget by editing the oracle's own config, with no redeploy of
# this policy or the agent bundle that references it.
#
# Because the thresholds live on the oracle side (not as factory_params),
# there is nothing to validate at factory-build time; instead every fetch
# defensively re-checks the strict ``0 < ask < limit`` invariant on both
# horizons before trusting the numbers (FINDING #1) — a violation is treated
# exactly like any other malformed oracle payload: fail open (ALLOW).
#
# FINDING #4: this factory's ASK-approval session_state keys are deliberately
# distinct from cost_budget's ``SESSION_COST_ASK_APPROVED_STATE_KEY``
# (``_policy_cost_ask_approved_usd``). Approving an LLM-spend ASK must never
# silently also approve a real-spend ASK (or vice versa) — they are
# independent guards over independent kinds of spend.

# The goettl-core Harness Status oracle's quota endpoint. Overridable via
# factory_params for non-default deployments; defaults to the documented
# localhost port the oracle listens on.
_REAL_SPEND_ORACLE_URL_DEFAULT = "http://localhost:5151/quota"

# Deliberately short: this GET runs inline in the policy hot path (blocking
# the request / tool_call phase), and per AB#2899 ANY failure to reach the
# oracle in time must fail open rather than stall the turn.
_REAL_SPEND_TIMEOUT_S_DEFAULT = 2.0

# The six numeric fields the oracle's /quota response must carry for the
# gate to trust it at all. Any missing key fails open (see
# ``_fetch_real_spend_quota``).
_REAL_SPEND_REQUIRED_KEYS = (
    "spend_today",
    "spend_mtd",
    "daily_ask",
    "daily_limit",
    "mtd_ask",
    "mtd_limit",
)

# session_state keys recording the highest spend value (USD) the user has
# already approved continuing past, per horizon. Distinct from
# ``SESSION_COST_ASK_APPROVED_STATE_KEY`` (cost_budget's key) by construction
# (FINDING #4) — approving one guard's ASK must never suppress the other's.
_REAL_SPEND_DAILY_ASK_APPROVED_KEY = "_policy_real_spend_budget_daily_ask_approved_usd"
_REAL_SPEND_MTD_ASK_APPROVED_KEY = "_policy_real_spend_budget_mtd_ask_approved_usd"


def _fetch_real_spend_quota(
    oracle_url: str,
    timeout_s: float,
    transport: httpx.BaseTransport | None,
) -> dict[str, float] | None:
    """GET the Harness Status oracle and return its validated numeric fields.

    Returns ``None`` on every failure mode the gate must fail open on — this
    is non-negotiable per AB#2899:

    - request timeout or connection error (any :class:`httpx.HTTPError`, or
      a bare :class:`OSError` from the underlying transport);
    - a non-200 HTTP response;
    - a response body that is not parseable JSON;
    - JSON that does not decode to an object (dict);
    - the oracle's own ``{"error": ...}`` stub response;
    - any of the six required keys missing;
    - any required value that is ``None``, a bool, or otherwise non-numeric;
    - a threshold pair that fails the strict ``0 < ask < limit`` invariant on
      EITHER horizon (FINDING #1) — treated as a malformed / degenerate
      oracle config, not a valid non-degenerate one.

    :param oracle_url: Full URL of the oracle's quota endpoint, e.g.
        ``"http://localhost:5151/quota"``.
    :param timeout_s: Request timeout in seconds. Kept short by design (see
        :data:`_REAL_SPEND_TIMEOUT_S_DEFAULT`).
    :param transport: Optional httpx transport override so tests mock the
        HTTP boundary; ``None`` uses the real network.
    :returns: A dict with the six keys in :data:`_REAL_SPEND_REQUIRED_KEYS`
        as floats, or ``None`` to signal "fail open".
    """
    try:
        with httpx.Client(transport=transport, timeout=timeout_s) as client:
            response = client.get(oracle_url)
    except httpx.HTTPError:
        return None
    except OSError:
        # Defensive: some transport-level connection failures surface as a
        # bare OSError rather than an httpx.HTTPError subclass.
        return None

    if response.status_code != 200:
        return None

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    if "error" in payload:
        # The oracle's own stub/error response (e.g. not yet configured) —
        # fail open, never DENY on a shape we can't trust.
        return None

    values: dict[str, float] = {}
    for key in _REAL_SPEND_REQUIRED_KEYS:
        if key not in payload:
            return None
        raw = payload[key]
        # bool is an int subclass in Python — exclude explicitly so a stray
        # `"daily_ask": true` doesn't get silently coerced to 1.0.
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        values[key] = float(raw)

    if not (0 < values["daily_ask"] < values["daily_limit"]):
        return None
    if not (0 < values["mtd_ask"] < values["mtd_limit"]):
        return None

    return values


def _real_spend_reason(
    *,
    verdict: str,
    phase: str,
    spend_today: float,
    daily_threshold: float,
    daily_tripped: bool,
    spend_mtd: float,
    mtd_threshold: float,
    mtd_tripped: bool,
) -> str:
    """Build the ASK / DENY reason for the real-spend budget gate.

    On the ``request`` phase the message is surfaced directly to the user
    (the turn never reaches the model), so it is the plain user-facing
    message. On the ``tool_call`` phase native harnesses hand the reason to
    the model, so it is phrased as a directive telling the agent to relay it
    verbatim and wait, matching the convention used by
    :func:`_over_budget_deny_reason` above.

    :param verdict: ``"ASK"`` or ``"DENY"`` — selects the wording.
    :param phase: The enforcement phase — ``"request"`` or ``"tool_call"``.
    :param spend_today: Current day spend (USD) from the oracle.
    :param daily_threshold: The daily threshold that was checked (ask or
        limit, matching *verdict*).
    :param daily_tripped: Whether the daily horizon tripped this verdict.
    :param spend_mtd: Current month-to-date spend (USD) from the oracle.
    :param mtd_threshold: The MTD threshold that was checked (ask or limit,
        matching *verdict*).
    :param mtd_tripped: Whether the MTD horizon tripped this verdict.
    :returns: The reason string.
    """
    horizon_label = "limit" if verdict == "DENY" else "warning threshold"
    parts = []
    if daily_tripped:
        parts.append(
            f"today's spend ${spend_today:.2f} reached the ${daily_threshold:.2f} daily "
            f"{horizon_label}"
        )
    if mtd_tripped:
        parts.append(
            f"month-to-date spend ${spend_mtd:.2f} reached the ${mtd_threshold:.2f} MTD "
            f"{horizon_label}"
        )
    detail = " and ".join(parts)
    if verdict == "DENY":
        verbatim = (
            f"Blocked by the real-spend budget policy: {detail}. Further spend-generating "
            f"actions are blocked until an operator raises the limit or the horizon rolls over."
        )
    else:
        verbatim = f"Real-spend warning: {detail}. Continue?"
    if phase == "request":
        return verbatim
    if verdict == "DENY":
        return (
            f"{verbatim} Relay this to the user verbatim, then stop and wait for them — do "
            f"not silently retry this tool call right now. This block is NOT permanent: once "
            f"an operator raises the relevant limit (or the day/month rolls over) and the user "
            f"asks you to continue, actually re-issue the tool call — do not just repeat this "
            f"message."
        )
    return (
        f"{verbatim} Relay this to the user verbatim and wait for their approval before "
        f"re-issuing this tool call."
    )


def real_spend_budget(
    oracle_url: str = _REAL_SPEND_ORACLE_URL_DEFAULT,
    timeout_s: float = _REAL_SPEND_TIMEOUT_S_DEFAULT,
    *,
    transport: httpx.BaseTransport | None = None,
) -> PolicyCallable:
    """Factory: gate on REAL (non-LLM) spend from the goettl-core oracle.

    Distinct from :func:`cost_budget` and its siblings: those gate on
    in-process LLM token spend; this gates on real business spend tracked
    entirely outside the engine by the goettl-core "Harness Status" oracle
    (``GET {oracle_url}``), which is expected to respond with
    ``spend_today``, ``spend_mtd``, and the four operator-set thresholds
    ``daily_ask`` / ``daily_limit`` / ``mtd_ask`` / ``mtd_limit`` (all USD).

    Gates the ``request`` phase (before the LLM turn, so text-only turns are
    covered too) and the ``tool_call`` phase (the native ``PreToolUse``
    block point) — abstains (ALLOW) on every other phase. On each gated
    call:

    - DENY when EITHER horizon's spend has reached its limit
      (``spend_today >= daily_limit`` or ``spend_mtd >= mtd_limit``);
    - else ASK when EITHER horizon's spend has reached its ask threshold
      (``spend_today >= daily_ask`` or ``spend_mtd >= mtd_ask``), unless the
      user has already approved continuing past the current spend level on
      every horizon that tripped (approval remembered via ``session_state``,
      keyed separately per horizon so a new, higher spend level re-asks);
    - else ALLOW.

    FAILS OPEN (returns ALLOW) on every oracle failure mode — timeout,
    connection error, non-200, malformed/unparseable JSON, a non-dict body,
    the oracle's own ``{"error": ...}`` stub, a missing expected key, a
    non-numeric/null value, or a threshold pair that fails the strict
    ``0 < ask < limit`` invariant on either horizon. This is non-negotiable
    per AB#2899: the guard should never block real work because of its own
    unavailability or misconfiguration.

    Uses its own ``session_state`` approval keys
    (:data:`_REAL_SPEND_DAILY_ASK_APPROVED_KEY` /
    :data:`_REAL_SPEND_MTD_ASK_APPROVED_KEY`), NOT
    ``SESSION_COST_ASK_APPROVED_STATE_KEY`` (cost_budget's key) — approving
    an LLM cost-budget ASK must never silently suppress this guard's
    prompts, or vice versa (FINDING #4).

    :param oracle_url: Full URL of the goettl-core Harness Status oracle's
        quota endpoint. Defaults to
        :data:`_REAL_SPEND_ORACLE_URL_DEFAULT`.
    :param timeout_s: Request timeout in seconds for the oracle GET. Must be
        ``> 0``. Defaults to :data:`_REAL_SPEND_TIMEOUT_S_DEFAULT`.
    :param transport: Optional httpx transport override so tests mock the
        HTTP boundary; ``None`` uses the real network. Not exposed via
        ``factory_params`` — a Python-only testing hook.
    :returns: A policy callable implementing the real-spend budget gate.
    :raises ValueError: If *oracle_url* is not a non-empty string, or
        *timeout_s* is not ``> 0``.
    """
    if not isinstance(oracle_url, str) or not oracle_url:
        raise ValueError(f"oracle_url must be a non-empty string, got {oracle_url!r}")
    if timeout_s <= 0:
        raise ValueError(f"timeout_s must be > 0, got {timeout_s!r}")

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        """Evaluate the real-spend budget for a request or tool call.

        :param event: Policy event dict.
        :returns: DENY when either horizon is at/over its limit; ASK when
            either horizon is at/over its ask threshold and not yet
            approved at the current spend level; ALLOW otherwise, including
            every oracle fail-open case.
        """
        phase = event.get("type")
        if phase not in _GATED_PHASES:
            return _ALLOW

        values = _fetch_real_spend_quota(oracle_url, timeout_s, transport)
        if values is None:
            return _ALLOW

        spend_today = values["spend_today"]
        spend_mtd = values["spend_mtd"]
        daily_ask, daily_limit = values["daily_ask"], values["daily_limit"]
        mtd_ask, mtd_limit = values["mtd_ask"], values["mtd_limit"]

        daily_deny = spend_today >= daily_limit
        mtd_deny = spend_mtd >= mtd_limit
        if daily_deny or mtd_deny:
            return {
                "result": "DENY",
                "reason": _real_spend_reason(
                    verdict="DENY",
                    phase=phase,
                    spend_today=spend_today,
                    daily_threshold=daily_limit,
                    daily_tripped=daily_deny,
                    spend_mtd=spend_mtd,
                    mtd_threshold=mtd_limit,
                    mtd_tripped=mtd_deny,
                ),
            }

        daily_over_ask = spend_today >= daily_ask
        mtd_over_ask = spend_mtd >= mtd_ask
        if daily_over_ask or mtd_over_ask:
            state = event.get("session_state") or {}
            daily_approved = float(state.get(_REAL_SPEND_DAILY_ASK_APPROVED_KEY, 0.0) or 0.0)
            mtd_approved = float(state.get(_REAL_SPEND_MTD_ASK_APPROVED_KEY, 0.0) or 0.0)
            daily_needs_ask = daily_over_ask and spend_today > daily_approved
            mtd_needs_ask = mtd_over_ask and spend_mtd > mtd_approved
            if daily_needs_ask or mtd_needs_ask:
                state_updates: list[StateUpdateEntry] = []
                if daily_needs_ask:
                    state_updates.append(
                        {
                            "key": _REAL_SPEND_DAILY_ASK_APPROVED_KEY,
                            "action": "set",
                            "value": spend_today,
                        }
                    )
                if mtd_needs_ask:
                    state_updates.append(
                        {
                            "key": _REAL_SPEND_MTD_ASK_APPROVED_KEY,
                            "action": "set",
                            "value": spend_mtd,
                        }
                    )
                return {
                    "result": "ASK",
                    "reason": _real_spend_reason(
                        verdict="ASK",
                        phase=phase,
                        spend_today=spend_today,
                        daily_threshold=daily_ask,
                        daily_tripped=daily_needs_ask,
                        spend_mtd=spend_mtd,
                        mtd_threshold=mtd_ask,
                        mtd_tripped=mtd_needs_ask,
                    ),
                    "state_updates": state_updates,
                }
        return _ALLOW

    return evaluate  # type: ignore[return-value]


# ── Registry ─────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.policies.builtins.cost.cost_budget",
        "kind": "factory",
        "name": "Session Cost Budget",
        "description": "Gates a session on cumulative LLM spend (USD): once a hard limit is "
        "reached DENY (the whole turn at the request phase, or each tool call) while still on "
        "an expensive model (prompting a /model downgrade), and ASK for approval at each soft "
        "warning checkpoint (request + tool-call phases). Reads "
        "event.context.usage.total_cost_usd and event.context.model.",
        "params_schema": {
            "type": "object",
            "properties": {
                "max_cost_usd": {
                    "type": "number",
                    "description": "Optional hard limit in USD; once cumulative session cost "
                    "reaches it, tool calls are blocked while the session is on an expensive "
                    "model. Either this or ask_thresholds_usd must be set.",
                },
                "ask_thresholds_usd": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Optional soft warning checkpoints in USD; the session asks "
                    "for approval the first time spend crosses each (every value must be < "
                    "max_cost_usd when both are set).",
                },
                "expensive_models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional case-insensitive substring tokens for the model "
                    "tiers blocked once over budget. Omit (or pass []) for a true hard stop "
                    "that blocks all models; pass a non-empty list for a downgrade gate that "
                    "only blocks the named tiers.",
                },
            },
        },
    },
    {
        "handler": "omnigent.policies.builtins.cost.user_daily_cost_budget",
        "kind": "factory",
        "name": "Per-User Daily Cost Budget",
        "description": "Gates the session OWNER's cumulative LLM spend across all their "
        "sessions for the current UTC day: once a hard daily limit is reached DENY (the whole "
        "turn at the request phase, or each tool call) while still on an expensive model "
        "(prompting a /model downgrade), and ASK for approval at each soft warning checkpoint "
        "(request + tool-call phases, remembered per user+day). Reads "
        "event.context.user_daily_cost and event.context.model.",
        "params_schema": {
            "type": "object",
            "properties": {
                "max_cost_usd": {
                    "type": "number",
                    "description": "Hard daily limit in USD; once the owner's spend for the "
                    "UTC day reaches it, tool calls are blocked while on an expensive model.",
                },
                "ask_thresholds_usd": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Optional soft daily warning checkpoints in USD; asks for "
                    "approval the first time the day's spend crosses each (every value must "
                    "be < max_cost_usd). Approval is remembered per user+day.",
                },
                "expensive_models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional case-insensitive substring tokens for the model "
                    "tiers blocked once over the daily budget. Omit (or pass []) for a true "
                    "hard stop that blocks all models; pass a non-empty list for a downgrade "
                    "gate that only blocks the named tiers.",
                },
            },
            "required": ["max_cost_usd"],
        },
    },
    {
        "handler": "omnigent.policies.builtins.cost.subagent_cost_budget",
        "kind": "factory",
        "name": "Subagent Cost Budget",
        "description": "Gates a sub-agent on its own subtree LLM spend (USD): once a hard limit "
        "is reached DENY (the whole turn at the request phase, or each tool call) while still on "
        "an expensive model (prompting a /model downgrade), and ASK for approval at each soft "
        "warning checkpoint (request + tool-call phases). Reads "
        "event.context.subtree_usage.total_cost_usd and event.context.model. Intended to be "
        "attached to a child session via sys_session_send's cost_budget argument.",
        "params_schema": {
            "type": "object",
            "properties": {
                "max_cost_usd": {
                    "type": "number",
                    "description": "Hard limit in USD for the subtree; once cumulative subtree "
                    "cost reaches it, tool calls are blocked while on an expensive model.",
                },
                "ask_thresholds_usd": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Optional soft warning checkpoints in USD; the subagent asks "
                    "for approval the first time subtree spend crosses each (every value must "
                    "be < max_cost_usd).",
                },
                "expensive_models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional case-insensitive substring tokens for the model "
                    "tiers blocked once over budget (default: Fable + Opus + GPT-5, excluding "
                    "the cheap -mini/-nano variants). An empty list disables the hard limit, "
                    "leaving only the soft thresholds.",
                },
            },
            "required": [],
        },
        "internal_only": True,
    },
    {
        "handler": "omnigent.policies.builtins.cost.real_spend_budget",
        "kind": "factory",
        "name": "Real Spend Budget (AB#2899)",
        "description": "Gates a session on REAL (non-LLM) business spend reported by the "
        "goettl-core Harness Status oracle: GETs spend_today / spend_mtd and the four "
        "operator-set thresholds (daily_ask, daily_limit, mtd_ask, mtd_limit) from the "
        "oracle's quota endpoint. DENYs (the whole turn at the request phase, or each tool "
        "call) when either the daily or MTD spend has reached its limit, ASKs for approval "
        "when either has reached its ask threshold (approval remembered per horizon so it "
        "does not re-prompt until spend rises further), and ALLOWs otherwise. FAILS OPEN "
        "(ALLOW) on any oracle failure — timeout, connection error, non-200, malformed JSON, "
        "missing/non-numeric fields, the oracle's own error stub, or a threshold pair that "
        "fails the strict 0 < ask < limit invariant — this is non-negotiable. Uses its own "
        "approval-state keys, distinct from the cost_budget policy's.",
        "params_schema": {
            "type": "object",
            "properties": {
                "oracle_url": {
                    "type": "string",
                    "description": "Full URL of the goettl-core Harness Status oracle's quota "
                    "endpoint. Defaults to http://localhost:5151/quota.",
                },
                "timeout_s": {
                    "type": "number",
                    "description": "Request timeout in seconds for the oracle GET. Must be > "
                    "0. Kept short since the guard fails open on timeout. Defaults to 2.0.",
                },
            },
            "required": [],
        },
    },
]
