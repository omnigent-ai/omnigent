"""Built-in token-budget policy.

A single factory, :func:`token_budget`, that gates a session on its
cumulative LLM token usage at the **request** phase (before the LLM
turn, so text-only turns are budgeted too) and the **tool-call** phase
(the point a native ``PreToolUse`` hook can block before the action
runs):

- ``ask_thresholds_tokens`` (optional, soft, **request + tool-call
  phases**): a list of warning checkpoints. The first time the session's
  cumulative tokens cross each checkpoint, the turn (request phase) or
  the tool call (tool-call phase) is parked for user approval (ASK).
  Each checkpoint prompts at most once *per approval* — the
  highest-approved checkpoint is remembered in ``session_state``, so
  approving lets usage continue to the next checkpoint. A decline blocks
  that one turn / tool call but does not record the checkpoint, so the
  next request or tool call over the same threshold re-asks until
  approved. (Both phases have a server-side approval round-trip that
  applies the ASK's ``state_updates`` only on accept — the request
  phase parks the whole turn before it reaches the model, so text-only
  turns are warned too.)
- ``max_tokens`` (optional, hard, **request + tool-call phases**): once
  cumulative usage reaches this, the policy DENYs, blocking the whole
  turn at the request phase, or each tool call. Both phases have a
  server-side approval round-trip.

It reads cumulative token usage from ``event["context"]["usage"]``,
summing ``input_tokens`` + ``output_tokens`` + ``cache_read_input_tokens``
+ ``cache_creation_input_tokens`` (to reflect actual token consumption
at the model, even though cached reads are billed at a lower rate). When
token usage is not reported by the executor, the gate **fails closed**
by ASKing, so operators can decide whether to allow unmetered spend.

**Why tokens, not cost**: Cost budgets (``cost_budget``) depend on
catalog pricing, and ACP agents' models frequently have none, so cost
cannot enforce there. Tokens are the vendor-neutral fallback, measuring
actual LLM consumption regardless of pricing availability.

**Additive behavior**: With no budget configured, behavior is identical
to today for every harness. The policy is stateless, firing only on
configured thresholds.

**Advisory, not a hard cap**: Executors self-report usage; an executor
that reports nothing cannot be metered. This gate surfaces that
unavailability explicitly (ASK for approval) rather than silently
treating missing usage as zero consumption.

YAML usage::

    policies:
      token_budget:
        type: function
        function:
          path: omnigent.policies.builtins.token_budget.token_budget
          arguments:
            max_tokens: 200000
            ask_thresholds_tokens: [100000, 150000]

The factory must be referenced via ``function: {path, arguments}`` with
a non-empty ``arguments`` block (the registry declares it
``kind: "factory"``).
"""

from __future__ import annotations

from collections.abc import Mapping

from omnigent.policies.schema import (
    PolicyCallable,
    PolicyEvent,
    PolicyResponse,
)

_ALLOW: PolicyResponse = {"result": "ALLOW"}

# session_state key recording the highest ``ask_thresholds_tokens``
# checkpoint the user has already approved continuing past (a token
# count; 0 when none). Set when an ASK is approved so each checkpoint
# prompts at most once. Shared with the engine, which routes it to the
# ROOT conversation so the approval covers the whole spawn tree (the
# budget is per-session, but a sub-agent runs as its own conversation).
_ASK_APPROVED_KEY = "token_budget_ask_approved"

# Phases the budget gate fires on. ``tool_call`` is the native
# ``PreToolUse`` block point; ``request`` runs before the LLM turn so
# text-only turns (no tool call) are budgeted too. Every other phase
# abstains (ALLOW).
_GATED_PHASES = frozenset({"request", "tool_call"})


def _as_int(value: object) -> int:
    """Coerce a usage counter (statically ``object``) to a non-negative int.

    Usage values may be missing, ``None``, or non-numeric on a malformed
    payload, so narrow before ``int()`` rather than catching at the call site.
    ``bool`` is an ``int`` subclass but never a token count, so it maps to 0.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def _session_tokens(event: PolicyEvent) -> int:
    """Read cumulative session tokens from a policy event.

    Prefers ``total_tokens`` when present; otherwise sums input_tokens +
    output_tokens + cache_read_input_tokens + cache_creation_input_tokens (to
    reflect actual consumption at the model, even though cached reads are
    billed at a lower rate). Malformed values coerce to 0.

    :param event: Policy event dict.
    :returns: ``total_tokens`` or the sum of components (int >= 0).
    """
    context = event.get("context") or {}
    usage = context.get("usage") or {}

    if "total_tokens" in usage:
        return _as_int(usage.get("total_tokens"))

    return (
        _as_int(usage.get("input_tokens"))
        + _as_int(usage.get("output_tokens"))
        + _as_int(usage.get("cache_read_input_tokens"))
        + _as_int(usage.get("cache_creation_input_tokens"))
    )


def _usage_has_tokens(usage: Mapping[str, object]) -> bool:
    """Return whether the usage dict contains any token data.

    Returns True when at least one of the token counters is present and
    non-zero (input_tokens, output_tokens, total_tokens,
    cache_read_input_tokens, cache_creation_input_tokens).

    :param usage: The usage dict from ``event["context"]["usage"]``.
    :returns: True when token data is present.
    """
    return bool(
        usage.get("input_tokens")
        or usage.get("output_tokens")
        or usage.get("total_tokens")
        or usage.get("cache_read_input_tokens")
        or usage.get("cache_creation_input_tokens")
    )


def token_budget(
    max_tokens: int | None = None,
    ask_thresholds_tokens: list[int] | None = None,
) -> PolicyCallable:
    """Factory: gate a session on cumulative LLM token usage.

    The hard limit (when set) gates BOTH the ``request`` phase (blocking
    the whole turn before the LLM runs, so text-only turns are budgeted
    too) and the ``tool_call`` phase: once the limit is reached, DENY.
    The soft warning checkpoints (ASK "continue?"; recorded on approve so
    they don't re-prompt, re-asked after a decline) fire on BOTH the
    ``request`` and ``tool_call`` phases — both have a server-side
    approval round-trip (see ``evaluate``). Abstains (ALLOW) on every
    other phase and whenever usage is not available.

    :param max_tokens: Optional hard limit in tokens. Once cumulative
        session tokens reach this, the whole turn (request phase) or each
        tool call (tool_call phase) is DENYed. Must be ``> 0`` if
        provided. Either this or *ask_thresholds_tokens* must be set.
    :param ask_thresholds_tokens: Optional soft warning checkpoints in
        tokens, e.g. ``[50000, 100000]``. Each ASKs for approval the
        first time cumulative tokens cross it (approval remembered via
        ``session_state``, so an approved checkpoint prompts at most
        once; a decline blocks the one turn / tool call and re-asks next
        time). ``None`` or ``[]`` disables the soft gate. Every value must
        be ``> 0`` and strictly less than *max_tokens* when both are set.
        Order does not matter — they are sorted internally.
    :returns: A policy callable implementing the token budget gate.
    :raises ValueError: If neither *max_tokens* nor *ask_thresholds_tokens*
        is provided, *max_tokens* is not positive, any
        *ask_thresholds_tokens* value is not in ``(0, max_tokens)`` when
        both are set.
    """
    if max_tokens is None and not ask_thresholds_tokens:
        raise ValueError("token_budget requires max_tokens and/or ask_thresholds_tokens")
    if max_tokens is not None and max_tokens <= 0:
        raise ValueError(f"max_tokens must be > 0, got {max_tokens!r}")
    thresholds = sorted({int(t) for t in (ask_thresholds_tokens or [])})
    for t in thresholds:
        if max_tokens is not None and not (0 < t < max_tokens):
            raise ValueError(
                f"each ask_thresholds_tokens value must be in "
                f"(0, max_tokens={max_tokens}), got {t!r}"
            )

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        """Evaluate the session token budget for a request or tool call.

        Gates the ``request`` phase (before the LLM turn, so text-only
        turns are budgeted too) and the ``tool_call`` phase (the native
        ``PreToolUse`` block) — abstains on every other phase.

        - ``tokens >= max_tokens`` → DENY (block turn / tool call).
          This hard gate runs on BOTH gated phases.
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
        :returns: DENY when over budget; ASK when a new soft checkpoint
            is newly crossed; ALLOW otherwise.
        """
        phase = event.get("type")
        if not isinstance(phase, str) or phase not in _GATED_PHASES:
            return _ALLOW
        context = event.get("context") or {}
        usage = context.get("usage") or {}
        if not _usage_has_tokens(usage):
            # Fail closed: no usage data available.
            return {
                "result": "ASK",
                "reason": (
                    "No token usage was reported for this turn. "
                    "The token budget cannot be enforced without usage data. "
                    "Approve to continue without budget enforcement, or request "
                    "a configuration change to ensure usage is reported."
                ),
            }
        tokens = _session_tokens(event)
        if max_tokens is not None and tokens >= max_tokens:
            return {
                "result": "DENY",
                "reason": f"Token budget exceeded: {tokens:,} tokens reached the hard limit "
                f"of {max_tokens:,} tokens.",
            }
        if thresholds:
            # Highest checkpoint the tokens have crossed so far.
            crossed = max((t for t in thresholds if tokens >= t), default=None)
            if crossed is not None:
                state = event.get("session_state") or {}
                approved_value = state.get(_ASK_APPROVED_KEY, 0)
                approved_up_to = (
                    int(approved_value) if isinstance(approved_value, int | float | str) else 0
                )
                if crossed > approved_up_to:
                    limit_str = f" (limit {max_tokens:,})" if max_tokens is not None else ""
                    return {
                        "result": "ASK",
                        "reason": (
                            f"Session tokens {tokens:,} passed the {crossed:,} "
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

    return evaluate
