"""Runner-side enforcement of function-type tool-call/tool-result policies.

Pre-refactor the Omnigent server's :class:`PolicyEngine` enforced
``function``-type policies (POLICIES.md §9.1) on every tool dispatch.
Post designs/RUNNER_MCP.md the runner owns MCP dispatch, so the
runner has to run these policies itself to keep parity.

Scope:

- ``function``-type policies with ``on:`` containing ``tool_call``
  or ``tool_result``. The example ``google_policy`` in
  ``examples/databricks_coding_agent.yaml`` is the canonical case.
- ``label`` and ``prompt`` types stay server-side — they need the
  ``ConversationStore`` and the LLM classifier respectively, which
  the runner doesn't have. Specs that wire those types to MCP tools
  do not get runner-side enforcement; the loud-failure design
  principle still applies: a DENY here returns the denial text as
  the tool output so the LLM sees the refusal cleanly.

ASK verdicts: the gate itself doesn't own an elicitation channel —
it just surfaces ASK to the caller. The caller (typically
``runner.tool_dispatch.execute_tool``) escalates by POSTing
``evaluate_policy=True`` to the Omnigent server, which independently
re-evaluates and parks an elicitation; the runner then awaits the
verdict via :mod:`omnigent.runner.pending_approvals`. The dual
evaluation (runner + server) is by design — the runner needs the
local fast-path for ALLOW/DENY, the server needs to own the
elicitation channel.

The gate is constructed per spec_hash and cached on the
:class:`RunnerMcpManager`. Stateless callables (factory + closure)
re-instantiate per spec; per-turn ``reset_turn`` is called when the
gate begins processing a new turn (the runner's tool_dispatch loop
calls :meth:`reset_turn` at the start of each ``proxy_stream`` turn).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from omnigent.policies import FunctionPolicy, resolve_function_policy
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.spec.types import (
    AgentSpec,
    FunctionPolicySpec,
    Phase,
    PolicyAction,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _GatedPolicy:
    """Pre-built FunctionPolicy + the phases it fires on."""

    name: str
    policy: FunctionPolicy
    phases: frozenset[Phase]


@dataclass(frozen=True)
class PolicyVerdict:
    """
    Typed verdict the runner-side gate returns to its caller.

    Replaces the prior ``str | None`` shape so callers can branch
    on ASK (and escalate to the Omnigent server) rather than treating
    every non-ALLOW outcome as DENY.

    :param action: ``"allow"`` (tool may proceed), ``"deny"`` (tool
        is blocked; ``deny_text`` carries the refusal to feed back
        to the harness), or ``"ask"`` (caller should escalate to
        the Omnigent server for a user verdict; ``policy_name`` and
        ``reason`` describe the prompt).
    :param deny_text: The full refusal payload (already wrapped via
        :func:`format_deny_text`) to feed back as the tool output when
        ``action == "deny"``. ``None`` for the other actions.
    :param policy_name: Identifier of the policy that produced
        this verdict, e.g. ``"google_policy"``. Populated for
        ``deny`` and ``ask`` actions (the deny case carries it so
        the dispatch layer can format an "ASK refused / timed
        out" payload in the same shape the gate produces). ``None``
        for ``allow``.
    :param reason: Reason text from the policy result. May be
        ``None`` even on ASK if the policy did not supply one.
    :param data: Replacement payload from the policy. On ALLOW
        verdicts, the caller substitutes this for the original
        event content — e.g. PII-redacted tool arguments
        (TOOL_CALL) or transformed output (TOOL_RESULT). ``None``
        when the policy did not return a replacement.
    """

    action: Literal["allow", "deny", "ask"]
    deny_text: str | None = None
    policy_name: str | None = None
    reason: str | None = None
    data: Any = None


# Singleton ALLOW verdict — frozen dataclass, no state, allocate
# once and share across every fast-path tool call.
_ALLOW: PolicyVerdict = PolicyVerdict(action="allow")


def _resolve_failure_diagnostic(ps: FunctionPolicySpec, exc: BaseException) -> str:
    """
    Build an actionable load-failure reason without embedding exception text.

    Factory kwargs (API keys, tokens) can appear in ``str(exc)``; keep only
    the exception type and the configured function path so operators can fix
    the spec without secrets landing in tool output.
    """
    path = ps.function.path if ps.function is not None else "<missing function>"
    return (
        f"policy failed to resolve ({type(exc).__name__}); "
        f"function path {path!r} could not be loaded; "
        f"tool calls are denied until this policy is fixed"
    )


def _unresolved_policy_sentinel(
    ps: FunctionPolicySpec,
    exc: BaseException,
) -> FunctionPolicy:
    """Fail-closed stand-in for a configured policy that failed to resolve."""
    reason = _resolve_failure_diagnostic(ps, exc)

    def _always_deny(_event: Any) -> dict[str, str]:
        return {"result": "DENY", "reason": reason}

    return FunctionPolicy(ps, _always_deny)


class RunnerToolPolicyGate:
    """Per-spec runner-side enforcement of function-type policies.

    Holds resolved :class:`FunctionPolicy` instances for every spec
    policy whose ``on:`` mentions ``TOOL_CALL`` or ``TOOL_RESULT``.
    Constructed once per spec_hash and reused across conversations.
    """

    def __init__(self, policies: list[_GatedPolicy]) -> None:
        """Construct from pre-resolved policies. Use :meth:`from_spec` for normal callers."""
        self._policies = policies

    @classmethod
    def from_spec(cls, spec: AgentSpec) -> RunnerToolPolicyGate:
        """Pick out function-type tool_call/tool_result policies and resolve them.

        A configured tool-phase policy that fails to resolve is replaced with
        a fail-closed sentinel that always DENYs. Skipping would leave an
        empty gate that ALLOWs every tool call (fail-open).
        """
        guard = getattr(spec, "guardrails", None)
        if guard is None or not guard.policies:
            return cls([])
        out: list[_GatedPolicy] = []
        for ps in guard.policies:
            if not isinstance(ps, FunctionPolicySpec):
                continue  # label / prompt stay server-side
            # on=None: function self-selects; include all tool phases.
            if ps.on is None:
                phases = frozenset([Phase.TOOL_CALL, Phase.TOOL_RESULT])
            else:
                phases = frozenset(s.phase for s in ps.on if _selector_covers_tools(s.phase))
            if not phases:
                continue
            try:
                policy = resolve_function_policy(ps)
            except Exception as exc:  # noqa: BLE001 - all resolution failures deny
                diagnostic = _resolve_failure_diagnostic(ps, exc)
                _logger.error("runner %s", diagnostic)
                policy = _unresolved_policy_sentinel(ps, exc)
                phases = frozenset([Phase.TOOL_CALL, Phase.TOOL_RESULT])
            out.append(_GatedPolicy(name=ps.name, policy=policy, phases=phases))
        return cls(out)

    def reset_turn(self) -> None:
        """Forward per-turn reset to stateful policy callables."""
        for gated in self._policies:
            gated.policy.reset_turn()

    @property
    def is_empty(self) -> bool:
        """True when no tool-phase function policies apply to this spec."""
        return not self._policies

    async def evaluate_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> PolicyVerdict:
        """
        Run TOOL_CALL policies; return the first non-ALLOW verdict.

        :param tool_name: Tool the harness is about to call,
            e.g. ``"google_search"``.
        :param arguments: Decoded JSON arguments passed to the tool.
        :returns: A :class:`PolicyVerdict`. ALLOW when no policy
            objected; DENY with denial text when any policy denied;
            ASK with ``policy_name`` + ``reason`` when a policy
            requested a user verdict.
        """
        if not self._policies:
            return _ALLOW
        ctx = EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": tool_name, "arguments": arguments},
            tool_name=tool_name,
        )
        return await self._evaluate_policies(ctx, Phase.TOOL_CALL)

    async def evaluate_tool_result(
        self,
        tool_name: str,
        output: str,
    ) -> str:
        """
        Run TOOL_RESULT policies; return denial text on DENY, else *output*.

        Tool-result evaluation is post-execution and the runner can
        only DENY (replacing the output) or ALLOW (passing it
        through). ASK on tool_result is treated as DENY because the
        output already exists — escalating to the user would block
        the harness with no clean rollback path. The behavior
        mirrors the prior gate contract.

        :param tool_name: Tool whose output we're evaluating.
        :param output: The tool's raw output string.
        :returns: The original ``output`` on ALLOW; a denial payload
            otherwise.
        """
        if not self._policies:
            return output
        ctx = EvaluationContext(
            phase=Phase.TOOL_RESULT,
            content=output,
            tool_name=tool_name,
        )
        verdict = await self._evaluate_policies(ctx, Phase.TOOL_RESULT)
        if verdict.action == "allow":
            # If the policy returned transformed output, use it instead.
            return verdict.data if verdict.data is not None else output
        if verdict.action == "deny":
            assert verdict.deny_text is not None
            return verdict.deny_text
        # ASK on tool_result is collapsed to DENY for the reason in
        # the docstring above. ``policy_name`` is always populated
        # for non-allow verdicts.
        assert verdict.policy_name is not None
        return format_deny_text(
            verdict.policy_name,
            verdict.reason
            or "approval required on tool_result but runner cannot prompt mid-flight",
        )

    async def _evaluate_policies(
        self,
        ctx: EvaluationContext,
        phase: Phase,
    ) -> PolicyVerdict:
        """
        Walk applicable policies in declaration order.

        Semantics mirror the AP-server ``PolicyEngine.evaluate`` loop:

        - DENY short-circuits immediately — no later policy can
          override a denial.
        - ASK is recorded but evaluation continues — a later policy
          may still escalate to DENY.
        - ALLOW continues to the next policy.
        - After all policies: return the first recorded ASK (if any),
          else ALLOW.

        :param ctx: Evaluation context built by the caller.
        :param phase: Phase being evaluated, e.g.
            :attr:`Phase.TOOL_CALL`.
        :returns: A :class:`PolicyVerdict`. ALLOW when every applicable
            policy passed; DENY when one denied (or raised); ASK
            when one requested a user verdict and no later policy
            denied.
        """
        pending_ask: PolicyVerdict | None = None
        # Last non-None data from any ALLOW-or-ASK result. Last write
        # wins — callers that need chained transforms compose in one callable.
        composed_data: Any = None
        for gated in self._policies:
            if phase not in gated.phases:
                continue
            try:
                result: PolicyResult = await gated.policy.evaluate(ctx, {})
            except Exception as exc:
                # Fail-closed: a raised policy denies the call rather
                # than silently allowing it. Matches the server-side
                # engine's wrapping behavior.
                _logger.exception(
                    "runner policy %r raised on %s; treating as DENY",
                    gated.name,
                    phase.value,
                )
                return PolicyVerdict(
                    action="deny",
                    deny_text=format_deny_text(
                        gated.name,
                        f"policy raised: {type(exc).__name__}: {exc}",
                    ),
                    policy_name=gated.name,
                )
            if result.action == PolicyAction.DENY:
                return PolicyVerdict(
                    action="deny",
                    deny_text=format_deny_text(gated.name, result.reason),
                    policy_name=gated.name,
                    reason=result.reason,
                )
            if result.data is not None:
                composed_data = result.data
            if result.action == PolicyAction.ASK and pending_ask is None:
                pending_ask = PolicyVerdict(
                    action="ask",
                    policy_name=gated.name,
                    reason=result.reason,
                    data=composed_data,
                )
        if pending_ask is not None:
            # Attach any data accumulated after the ASK verdict.
            return PolicyVerdict(
                action="ask",
                policy_name=pending_ask.policy_name,
                reason=pending_ask.reason,
                data=composed_data,
            )
        if composed_data is not None:
            return PolicyVerdict(action="allow", data=composed_data)
        return _ALLOW


def _selector_covers_tools(phase: Phase) -> bool:
    """True iff *phase* is a tool-dispatch phase the runner can enforce."""
    return phase in (Phase.TOOL_CALL, Phase.TOOL_RESULT)


def format_deny_text(policy_name: str, reason: str | None) -> str:
    """
    Format a deny payload the LLM sees as the tool output.

    Public because the dispatch layer also needs to format
    denials — when an ASK round-trip returns refused / times
    out, the tool result is a denial with the same shape the
    gate produces locally for DENY verdicts. Centralizing the
    format here keeps the LLM-visible refusal shape uniform.

    :param policy_name: Identifier of the policy producing the
        refusal, e.g. ``"google_policy"``.
    :param reason: Free-text reason from the policy result. May
        be ``None`` if the policy didn't supply one; falls back
        to ``"policy denied"``.
    :returns: A bracketed prefix + JSON body the LLM can read,
        e.g. ``"[Denied by policy: google_policy] {...}"``.
    """
    body = {
        "denied_by_policy": policy_name,
        "reason": reason or "policy denied",
    }
    return f"[Denied by policy: {policy_name}] {json.dumps(body)}"
