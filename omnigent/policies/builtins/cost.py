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
the agent spec's ``llm.model``, resolved by the policy engine). When
pricing is unavailable the cost stays ``0.0`` and the policy never trips
(it cannot budget what it cannot price).

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

from dataclasses import dataclass
from typing import Any

from omnigent.policies.schema import (
    SESSION_COST_ASK_APPROVED_STATE_KEY,
    USER_DAILY_ASK_APPROVED_STATE_KEY,
    PolicyCallable,
    PolicyEvent,
    PolicyResponse,
)

_ALLOW: PolicyResponse = {"result": "ALLOW"}

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

# Default substring tokens (case-insensitive) identifying the "expensive"
# model tiers that must be downgraded once the session passes
# ``max_cost_usd``. Matched as substrings of the active model id so a single
# token hits every deployment spelling AND the tier aliases the native
# ``/model`` command stores: ``"opus"`` matches ``claude-opus-4-8``,
# ``databricks-claude-opus-4-7``, and the bare alias ``"opus"``; ``"gpt-5"``
# matches the whole GPT-5 family (``gpt-5``, ``gpt-5.5``, the dash spelling
# ``databricks-gpt-5-5``, …) EXCEPT the cheap variants carved out by
# ``_DEFAULT_EXPENSIVE_EXCLUDES`` below. Fable (``claude-fable-5``, the tier
# above Opus at 2x its price), Opus, and GPT-5 are the costly tiers today.
_DEFAULT_EXPENSIVE_MODELS = ("fable", "opus", "gpt-5")

# Default substring tokens (case-insensitive) that OVERRIDE a match in
# ``_DEFAULT_EXPENSIVE_MODELS`` back to "not expensive". The broad ``"gpt-5"``
# token above also matches the cheap ``gpt-5-mini`` / ``gpt-5-nano`` variants,
# which should NOT be budget-blocked, so they are excluded here. The leading
# dash is deliberate: a bare ``"mini"`` would also match unrelated models like
# ``gemini``. Applied only to the built-in default set — when the caller passes
# an explicit ``expensive_models`` list, those tokens are matched literally
# with no exclusions (the caller controls the set exactly).
_DEFAULT_EXPENSIVE_EXCLUDES = ("-mini", "-nano")


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
    expensive_list = ", ".join(expensive_tokens) or "the configured high-cost models"
    if harness is not None and "codex" in harness:
        switch_hint = "in the terminal, run /model and pick a cheaper model to continue"
    else:
        switch_hint = "switch to a cheaper model to continue"
    spend_subject = f"{subject_user}'s spend" if subject_user else "spend"
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

    :param model: The active model id, or ``None`` when
        undeterminable.
    :param expensive_tokens: Lowercased substring tokens identifying
        expensive models, e.g. ``("opus", "gpt-5")``.
    :param exclude_tokens: Lowercased substring tokens that override an
        expensive match back to "not expensive", e.g.
        ``("-mini", "-nano")``. Defaults to empty (no exclusions).
    :returns: ``True`` when tool calls should be DENYed over budget.
    """
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
        active. ``False`` only when the caller passes an empty
        ``expensive_models`` list (soft thresholds only).
    """

    expensive_tokens: tuple[str, ...]
    exclude_tokens: tuple[str, ...]
    hard_cap_enabled: bool


def _resolve_expensive_models(expensive_models: list[str] | None) -> _ExpensiveModelConfig:
    """Resolve the ``expensive_models`` factory argument into matching config.

    Shared by :func:`cost_budget` and :func:`user_daily_cost_budget` so
    both treat the argument identically:

    - ``None`` → the built-in default set
      (:data:`_DEFAULT_EXPENSIVE_MODELS`) with the cheap-variant
      exclusions (:data:`_DEFAULT_EXPENSIVE_EXCLUDES`) applied; hard gate
      on.
    - a non-empty list → those tokens (lowercased), matched literally
      with NO exclusions (the caller controls the set exactly); hard gate
      on.
    - ``[]`` → no expensive tokens; hard gate off (soft thresholds only).

    :param expensive_models: The factory argument, e.g.
        ``["opus", "gpt-5"]``, ``None``, or ``[]``.
    :returns: The resolved :class:`_ExpensiveModelConfig`.
    :raises ValueError: If any entry is not a non-empty string.
    """
    if expensive_models is None:
        return _ExpensiveModelConfig(
            expensive_tokens=_DEFAULT_EXPENSIVE_MODELS,
            exclude_tokens=_DEFAULT_EXPENSIVE_EXCLUDES,
            hard_cap_enabled=True,
        )
    for m in expensive_models:
        if not isinstance(m, str) or not m:
            raise ValueError(f"each expensive_models value must be a non-empty string, got {m!r}")
    expensive_tokens = tuple(m.lower() for m in expensive_models)
    return _ExpensiveModelConfig(
        expensive_tokens=expensive_tokens,
        exclude_tokens=(),
        hard_cap_enabled=len(expensive_tokens) > 0,
    )


def cost_budget(
    max_cost_usd: float,
    ask_thresholds_usd: list[float] | None = None,
    expensive_models: list[str] | None = None,
) -> PolicyCallable:
    """Factory: gate a session on cumulative LLM spend (USD).

    The hard limit gates BOTH the ``request`` phase (blocking the whole
    turn before the LLM runs, so text-only turns are budgeted too) and
    the ``tool_call`` phase: once the limit is reached, DENY while the
    session is still on an expensive model — telling the user to
    ``/model`` to a cheaper one. The soft warning checkpoints (ASK
    "continue?"; recorded on approve so they don't re-prompt, re-asked
    after a decline) fire on BOTH the ``request`` and ``tool_call``
    phases — both have a server-side approval round-trip (see
    ``evaluate``). Abstains (ALLOW) on every other phase and whenever
    cost is unpriced (``0.0``).

    :param max_cost_usd: Hard limit in USD. Once cumulative session cost
        reaches this, tool calls are DENYed while the session is on an
        expensive model, e.g. ``5.0``. Must be ``> 0``.
    :param ask_thresholds_usd: Optional soft warning checkpoints in USD,
        e.g. ``[1.0, 2.5]``. Each ASKs for approval the first time
        cumulative cost crosses it (approval remembered via
        ``session_state``, so an approved checkpoint prompts at most
        once; a decline blocks the one turn / tool call and re-asks next
        time). ``None`` or ``[]`` disables the soft gate. Every value must be
        ``> 0`` and strictly less than *max_cost_usd*. Order does not
        matter — they are sorted internally.
    :param expensive_models: Optional case-insensitive substring tokens
        identifying the model tiers blocked once over *max_cost_usd*,
        e.g. ``["opus", "gpt-5"]``. A token matches when it is a
        substring of the active model id (so ``"opus"`` matches both
        ``"databricks-claude-opus-4-8"`` and the alias ``"opus"``).
        ``None`` uses the built-in default (Fable + Opus + GPT-5,
        excluding the cheap ``-mini`` / ``-nano`` variants). An explicit
        list is matched literally with no exclusions. ``[]`` disables the
        hard gate entirely (soft thresholds only, no budget block). Each
        value must be a non-empty string.
    :returns: A policy callable implementing the budget gate.
    :raises ValueError: If *max_cost_usd* is not positive, any
        *ask_thresholds_usd* value is not in ``(0, max_cost_usd)``, or
        any *expensive_models* entry is not a non-empty string.
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
        cost = _session_cost_usd(event)
        if cfg.hard_cap_enabled and cost >= max_cost_usd:
            if _model_blocked_over_budget(
                _current_model(event), cfg.expensive_tokens, cfg.exclude_tokens
            ):
                return {
                    "result": "DENY",
                    "reason": _over_budget_deny_reason(
                        cost,
                        max_cost_usd,
                        cfg.expensive_tokens,
                        _current_harness(event),
                        phase=phase,
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
                    return {
                        "result": "ASK",
                        "reason": (
                            f"Session cost ${cost:.2f} passed the ${crossed:.2f} "
                            f"warning threshold (limit ${max_cost_usd:.2f}). Continue?"
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
        uses the built-in default (Fable + Opus + GPT-5, excluding the
        cheap ``-mini`` / ``-nano`` variants); an explicit list is
        matched literally with no exclusions; ``[]`` disables the hard
        gate (soft thresholds only).
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
        cost = _user_daily_cost_usd(event)
        owner = _user_daily_owner(event)
        if cfg.hard_cap_enabled and cost >= max_cost_usd:
            if _model_blocked_over_budget(
                _current_model(event), cfg.expensive_tokens, cfg.exclude_tokens
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
        cost = _subtree_cost_usd(event)
        # Check hard limit if max_cost_usd is set.
        if max_cost_usd is not None and cfg.hard_cap_enabled and cost >= max_cost_usd:
            if _model_blocked_over_budget(
                _current_model(event), cfg.expensive_tokens, cfg.exclude_tokens
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
                    "description": "Hard limit in USD; once cumulative session cost reaches "
                    "it, tool calls are blocked while the session is on an expensive model.",
                },
                "ask_thresholds_usd": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Optional soft warning checkpoints in USD; the session asks "
                    "for approval the first time spend crosses each (every value must be < "
                    "max_cost_usd).",
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
            "required": ["max_cost_usd"],
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
                    "tiers blocked once over the daily budget (default: Fable + Opus + GPT-5, "
                    "excluding the cheap -mini/-nano variants). An empty list disables the hard "
                    "limit, leaving only the soft thresholds.",
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
]
