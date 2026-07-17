"""
Policy building blocks — pure evaluators, no runtime state.

This package holds the pieces an agent author (or the parser)
reaches for when declaring / implementing a policy:

- :class:`EvaluationContext`, :class:`PolicyResult`,
  :class:`ElicitationRequest` — the data shapes that cross the
  evaluate boundary (see :mod:`omnigent.policies.types`).
- :class:`Policy` ABC (:mod:`omnigent.policies.base`) and
  the concrete subclass :class:`FunctionPolicy`.

The subclasses are pure in the important sense: they own no
mutable state across calls, do no DB I/O, and don't know about
conversations. State (label cache, conversation id,
write-through store) and orchestration (composition loop, ASK
parking, fail-closed) live in :mod:`omnigent.runtime.policies`.

Agent-author callables should import :class:`EvaluationContext`
and :class:`PolicyResult` from here, not from
``omnigent.spec.types`` — those are runtime evaluation
artifacts, not declarations that appear in a spec.
"""

from __future__ import annotations

from omnigent.admission import AdmissionInfo
from omnigent.policies.base import Policy
from omnigent.policies.function import (
    FunctionPolicy,
    resolve_function_policy,
)
from omnigent.policies.schema import (
    PolicyCallable,
    PolicyCallableWithConfig,
    PolicyEvent,
    PolicyResponse,
    StateUpdateEntry,
)
from omnigent.policies.types import (
    ElicitationRequest,
    EvaluationContext,
    PolicyLLMClient,
    PolicyResult,
)

__all__ = [
    "AdmissionInfo",
    "ElicitationRequest",
    "EvaluationContext",
    "FunctionPolicy",
    "Policy",
    "PolicyCallable",
    "PolicyCallableWithConfig",
    "PolicyEvent",
    "PolicyLLMClient",
    "PolicyResponse",
    "PolicyResult",
    "StateUpdateEntry",
    "resolve_function_policy",
]
