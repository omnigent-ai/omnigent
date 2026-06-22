"""
Unit tests for :mod:`omnigent.runtime.policies.engine` helpers.

Covers the ``_fail_closed`` and ``_dispatch_policy`` functions, with
particular focus on the interaction between declared action lists and
the fail-closed fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.runtime.policies.engine import _dispatch_policy, _fail_closed
from omnigent.spec.types import (
    FunctionPolicySpec,
    Phase,
    PolicyAction,
)

# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


def _make_spec(
    name: str = "test-policy",
    action: list[PolicyAction] | None = None,
) -> FunctionPolicySpec:
    """
    Build a minimal :class:`FunctionPolicySpec` for testing.

    :param name: Policy name.
    :param action: Declared action whitelist.
    :returns: A :class:`FunctionPolicySpec` with only the fields
        relevant to ``_fail_closed`` populated.
    """
    return FunctionPolicySpec(name=name, on=None, action=action)


@dataclass
class _StubPolicy:
    """
    Minimal stand-in for a :class:`Policy` usable by
    ``_dispatch_policy``.

    :param spec: The policy spec.
    :param exc: If set, ``evaluate`` raises this instead of
        returning a result.
    :param result: The result to return from ``evaluate`` when
        *exc* is ``None``.
    """

    spec: FunctionPolicySpec
    exc: Exception | None = None
    result: PolicyResult | None = None

    async def evaluate(self, ctx: EvaluationContext, context: dict[str, Any]) -> PolicyResult:
        """
        Fake evaluate that either raises or returns a canned result.

        :param ctx: Ignored.
        :param context: Ignored.
        :returns: ``self.result`` when ``self.exc`` is ``None``.
        :raises Exception: ``self.exc`` when set.
        """
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


# ---------------------------------------------------------------------------
# _fail_closed — unit tests
# ---------------------------------------------------------------------------


def test_fail_closed_no_declared_actions_returns_deny() -> None:
    """No declared action list → default DENY."""
    spec = _make_spec(action=None)
    result = _fail_closed(spec, reason="boom")
    assert result.action == PolicyAction.DENY
    assert result.reason == "boom"


def test_fail_closed_allow_only_returns_allow() -> None:
    """Classifier-only ([allow]) → substitute ALLOW."""
    spec = _make_spec(action=[PolicyAction.ALLOW])
    result = _fail_closed(spec, reason="boom")
    assert result.action == PolicyAction.ALLOW
    assert result.reason is None


def test_fail_closed_deny_in_list_returns_deny() -> None:
    """Declared [allow, deny] → DENY (DENY is available)."""
    spec = _make_spec(action=[PolicyAction.ALLOW, PolicyAction.DENY])
    result = _fail_closed(spec, reason="boom")
    assert result.action == PolicyAction.DENY


def test_fail_closed_ask_only_returns_ask() -> None:
    """
    Approval-gate ([ask]) → park for approval (ASK).

    Regression: previously this returned ALLOW because the
    predicate only checked for the absence of DENY.
    """
    spec = _make_spec(action=[PolicyAction.ASK])
    result = _fail_closed(spec, reason="evaluator timeout")
    assert result.action == PolicyAction.ASK
    assert result.reason == "evaluator timeout"


def test_fail_closed_allow_ask_returns_ask() -> None:
    """
    Approval-gate ([allow, ask]) → park for approval (ASK).

    Regression: previously this returned ALLOW.
    """
    spec = _make_spec(action=[PolicyAction.ALLOW, PolicyAction.ASK])
    result = _fail_closed(spec, reason="callable error")
    assert result.action == PolicyAction.ASK
    assert result.reason == "callable error"


def test_fail_closed_ask_deny_returns_deny() -> None:
    """[ask, deny] → DENY wins (DENY present in declared list)."""
    spec = _make_spec(action=[PolicyAction.ASK, PolicyAction.DENY])
    result = _fail_closed(spec, reason="boom")
    assert result.action == PolicyAction.DENY


def test_fail_closed_all_actions_returns_deny() -> None:
    """[allow, ask, deny] → DENY (DENY is present)."""
    spec = _make_spec(
        action=[PolicyAction.ALLOW, PolicyAction.ASK, PolicyAction.DENY],
    )
    result = _fail_closed(spec, reason="boom")
    assert result.action == PolicyAction.DENY


# ---------------------------------------------------------------------------
# _dispatch_policy — integration with _fail_closed on exceptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_policy_exception_ask_policy_parks() -> None:
    """
    An [ask] policy whose evaluator raises must yield ASK (park for
    approval), not ALLOW.

    This is the core regression scenario: an approval-gate policy
    that errors out should not bypass the gate.
    """
    spec = _make_spec(name="approval-gate", action=[PolicyAction.ASK])
    stub = _StubPolicy(spec=spec, exc=RuntimeError("evaluator exploded"))
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "dangerous_tool", "arguments": {}},
        tool_name="dangerous_tool",
    )
    result = await _dispatch_policy(stub, ctx, {})
    assert result.action == PolicyAction.ASK, (
        "approval-gate policy that raises must park (ASK), not allow"
    )
    assert "evaluator exploded" in (result.reason or "")


@pytest.mark.asyncio
async def test_dispatch_policy_exception_allow_only_substitutes_allow() -> None:
    """Classifier-only [allow] policy that raises → ALLOW (unchanged)."""
    spec = _make_spec(name="advisory", action=[PolicyAction.ALLOW])
    stub = _StubPolicy(spec=spec, exc=ValueError("oops"))
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "safe_tool", "arguments": {}},
        tool_name="safe_tool",
    )
    result = await _dispatch_policy(stub, ctx, {})
    assert result.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_dispatch_policy_exception_no_declared_denies() -> None:
    """No declared action list + exception → DENY."""
    spec = _make_spec(name="strict", action=None)
    stub = _StubPolicy(spec=spec, exc=TypeError("bad"))
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "tool", "arguments": {}},
        tool_name="tool",
    )
    result = await _dispatch_policy(stub, ctx, {})
    assert result.action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_dispatch_policy_disallowed_return_ask_policy_parks() -> None:
    """
    An [ask] policy that returns DENY (not in its declared list)
    should fail-closed to ASK, not ALLOW.
    """
    spec = _make_spec(name="gate", action=[PolicyAction.ASK])
    bad_result = PolicyResult(
        action=PolicyAction.DENY,
        reason="should not be permitted",
    )
    stub = _StubPolicy(spec=spec, result=bad_result)
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "tool", "arguments": {}},
        tool_name="tool",
    )
    result = await _dispatch_policy(stub, ctx, {})
    assert result.action == PolicyAction.ASK
