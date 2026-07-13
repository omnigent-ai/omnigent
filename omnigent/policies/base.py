"""
Abstract base class for policy evaluator instances.

A :class:`Policy` is an instantiated, per-workflow evaluator
derived from a :class:`PolicySpec`. Subclasses implement one
:meth:`evaluate` method that returns a :class:`PolicyResult`;
the engine (in :mod:`omnigent.runtime.policies.engine`) does
the filter-gate-dispatch-compose orchestration (see
POLICIES.md §4).

The two concrete subclasses live next to this module:

- :class:`omnigent.policies.function.FunctionPolicy`
- :class:`omnigent.policies.prompt.PromptPolicy`

These classes are pure evaluators — they hold no mutable state
across calls, do no DB I/O, and don't know about conversations.
Mutable runtime state (label cache, conversation id,
write-through store) and the composition loop live in
:mod:`omnigent.runtime.policies`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.spec.types import PolicySpec


class Policy(ABC):
    """
    Per-workflow policy instance.

    Subclasses declare their ``spec`` attribute (subclass of
    :class:`PolicySpec`) and implement :meth:`evaluate`. The
    engine calls :meth:`evaluate` only when the spec's
    selector and condition gates match the current context;
    implementations therefore don't need to re-check those.

    :param spec: The declarative :class:`PolicySpec` (or
        subclass) this policy was built from. Concrete
        subclasses narrow the type.
    """

    spec: PolicySpec

    @abstractmethod
    async def evaluate(
        self,
        ctx: EvaluationContext,
        context: dict[str, Any],
    ) -> PolicyResult:
        """
        Return this policy's decision for one evaluation.

        :param ctx: Current evaluation context — phase,
            content, resolved tool_name. Immutable; the
            caller built it from whatever local state the
            enforcement site had.
        :param context: Read-only context bundle from the
            engine — labels snapshot, conversation_id, and
            other identity fields policy callables may want
            to inspect. Structured as a plain dict to keep
            the FunctionPolicy callable contract compatible
            with omnigent' signature (see POLICIES.md §9.1).
        :returns: The policy's single-policy
            :class:`PolicyResult` (``deciding_policy`` left
            ``None`` — engine fills it on composed results).
        """

    def reset_turn(self) -> None:  # noqa: B027 — intentional no-op default (see docstring)
        """
        Reset per-turn state, if any.

        Stateless policies (the default) ignore this. Stateful
        policies whose author callable advertises per-turn
        accumulators — e.g. the ``max_tool_calls_per_turn``
        rate-limit factory in
        ``examples/_shared/rate_limit_policy.py`` — override
        this to clear those counters at the start of each
        turn. Mirrors :meth:`omnigent.runtime.policies.engine.PolicyEngine.reset_turn`.

        The runtime calls this once per "turn" — defined as one
        user prompt → terminal assistant response cycle, which
        in omnigent corresponds to one ``_run_agent_loop``
        invocation. Sub-iteration steps (tool calls within a
        turn) do NOT trigger a reset.

        Deliberately a concrete no-op (not ``@abstractmethod``)
        so subclasses opt INTO per-turn lifecycle handling
        rather than being forced to override; same convention
        as :meth:`omnigent.runtime.policies.engine.PolicyEngine.reset_turn`.
        """
