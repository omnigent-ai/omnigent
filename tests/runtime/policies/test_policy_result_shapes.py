"""
PolicyResult shape tests — dataclass invariants.

Verifies the contract every consumer of :class:`PolicyResult`
relies on: immutability, defensive equality, defaults, and
the deciding_policy attribution rules for composed results.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.spec.types import (
    Phase,
    PolicyAction,
)

# ── Frozen / immutable ────────────────────────────────


def test_policy_result_is_frozen() -> None:
    """PolicyResult is @dataclass(frozen=True) so consumers
    can safely store it without defensive copies. Mutation
    raises. If this regresses, the engine's composed
    results could be mutated by observers and corrupt later
    evaluations."""
    r = PolicyResult(action=PolicyAction.ALLOW)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        r.action = PolicyAction.DENY  # type: ignore[misc]


# ── Default field values ──────────────────────────────


def test_policy_result_defaults() -> None:
    """Minimal construction sets sensible defaults."""
    r = PolicyResult(action=PolicyAction.ALLOW)
    assert r.action == PolicyAction.ALLOW
    assert r.reason is None
    assert r.set_labels is None
    assert r.deciding_policy is None
    assert r.deciding_policies is None


def test_policy_result_full_construction() -> None:
    """All fields settable; equality holds component-wise."""
    r1 = PolicyResult(
        action=PolicyAction.DENY,
        reason="blocked",
        set_labels={"a": "1"},
        deciding_policies=["p"],
    )
    r2 = PolicyResult(
        action=PolicyAction.DENY,
        reason="blocked",
        set_labels={"a": "1"},
        deciding_policies=["p"],
    )
    # Equality is structural — two results with the same
    # fields compare equal.
    assert r1 == r2


def test_policy_result_inequality_on_action() -> None:
    """Different actions → not equal, even with same reason / labels."""
    a = PolicyResult(action=PolicyAction.ALLOW, reason="x")
    b = PolicyResult(action=PolicyAction.DENY, reason="x")
    assert a != b


def test_policy_result_inequality_on_deciding_policy() -> None:
    """deciding_policy (derived from deciding_policies[0]) participates
    in equality via deciding_policies — used by observability to
    distinguish identical-reason results from different sources."""
    a = PolicyResult(action=PolicyAction.DENY, deciding_policies=["a"])
    b = PolicyResult(action=PolicyAction.DENY, deciding_policies=["b"])
    assert a != b
    assert a.deciding_policy == "a"
    assert b.deciding_policy == "b"


# ── Evaluation context shape ──────────────────────────


def test_evaluation_context_is_frozen() -> None:
    """EvaluationContext is frozen — contexts pass through
    multiple layers (engine → policy → condition); mutation
    by any consumer would corrupt the shared view."""
    ctx = EvaluationContext(phase=Phase.REQUEST, content="x")
    with pytest.raises((FrozenInstanceError, AttributeError)):
        ctx.phase = Phase.RESPONSE  # type: ignore[misc]


def test_evaluation_context_defaults() -> None:
    """Minimal construction: phase + content required;
    tool_name defaults to None."""
    ctx = EvaluationContext(phase=Phase.REQUEST, content="x")
    assert ctx.tool_name is None


def test_evaluation_context_with_tool_name() -> None:
    """Tool-phase context carries resolved tool_name."""
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"tool": "web_search"},
        tool_name="web_search",
    )
    assert ctx.tool_name == "web_search"


# ── Phase enum ────────────────────────────────────────


def test_phase_values_stable() -> None:
    """Phase enum values match the wire format exactly.
    Clients / spec YAML use these strings; any rename
    would silently break every deployed spec."""
    assert Phase.REQUEST.value == "request"
    assert Phase.TOOL_CALL.value == "tool_call"
    assert Phase.TOOL_RESULT.value == "tool_result"
    assert Phase.RESPONSE.value == "response"
    assert Phase.LLM_REQUEST.value == "llm_request"
    assert Phase.LLM_RESPONSE.value == "llm_response"


def test_phase_iteration_order() -> None:
    """Iteration order follows the agent loop: REQUEST →
    TOOL_CALL → TOOL_RESULT → RESPONSE, then the LLM phases
    (LLM_REQUEST → LLM_RESPONSE). Stable order matters for
    observability dashboards and debug output that iterate phases
    in a natural sequence."""
    assert list(Phase) == [
        Phase.REQUEST,
        Phase.TOOL_CALL,
        Phase.TOOL_RESULT,
        Phase.RESPONSE,
        Phase.LLM_REQUEST,
        Phase.LLM_RESPONSE,
    ]


def test_phase_str_mixin_works() -> None:
    """Phase inherits str so `Phase.REQUEST == "request"` is
    True — useful for JSON serialization / logging without
    explicit `.value` calls."""
    # Explicit == tests the str mix-in.
    assert Phase.REQUEST == "request"
    assert Phase.REQUEST == "request"
    # And str() produces the value.
    assert str(Phase.REQUEST.value) == "request"


# ── PolicyAction enum ─────────────────────────────────


def test_policy_action_values_stable() -> None:
    """PolicyAction enum values are spec-facing (YAML uses
    these strings). Renames break every deployed spec."""
    assert PolicyAction.ALLOW.value == "allow"
    assert PolicyAction.ASK.value == "ask"
    assert PolicyAction.DENY.value == "deny"


def test_policy_action_from_string() -> None:
    """Constructing a PolicyAction from a string works —
    the parser's action-list handler relies on this."""
    assert PolicyAction("allow") == PolicyAction.ALLOW
    assert PolicyAction("ask") == PolicyAction.ASK
    assert PolicyAction("deny") == PolicyAction.DENY


def test_policy_action_invalid_string_raises() -> None:
    """Unknown action string → ValueError. The parser
    catches this at spec-load to fail loud."""
    with pytest.raises(ValueError):
        PolicyAction("approve")
