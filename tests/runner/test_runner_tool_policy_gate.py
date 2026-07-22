"""Tests for :class:`RunnerToolPolicyGate` resolve-time fail-closed behavior.

A configured tool-phase policy that fails to resolve must not disappear
silently: ``from_spec`` installs a sentinel that DENYs on TOOL_CALL and
TOOL_RESULT. Successfully resolved policies keep their existing verdicts.
"""

from __future__ import annotations

import json

import pytest

from omnigent.runner.policy import RunnerToolPolicyGate
from omnigent.spec.types import (
    AgentSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    Phase,
    PhaseSelector,
)

_BROKEN_PATH = "omnigent.nonexistent_module.broken_policy"
_FIXED_ALLOW = FunctionRef(
    path="omnigent.policies.function.make_fixed_action_callable",
    arguments={"action": "allow"},
)
_FIXED_DENY = FunctionRef(
    path="omnigent.policies.function.make_fixed_action_callable",
    arguments={"action": "deny", "reason": "blocked by valid policy"},
)
_RAISING_PATH = "omnigent.policies.function.make_fixed_action_callable"


def _agent_with_policies(*policies: FunctionPolicySpec) -> AgentSpec:
    """Minimal agent whose guardrails carry the given function policies."""
    return AgentSpec(
        spec_version=1,
        name="runner-policy-gate-test",
        guardrails=GuardrailsSpec(policies=list(policies)),
    )


def _tool_policy(
    name: str,
    function: FunctionRef,
    *,
    phases: list[Phase] | None = None,
) -> FunctionPolicySpec:
    """Build a function policy that fires on the given tool phases."""
    if phases is None:
        on = None
    else:
        on = [PhaseSelector(phase=p) for p in phases]
    return FunctionPolicySpec(name=name, on=on, function=function)


def _broken_policy(name: str = "broken") -> FunctionPolicySpec:
    """Configured tool policy whose function path cannot be imported."""
    return _tool_policy(name, FunctionRef(path=_BROKEN_PATH))


@pytest.mark.asyncio
async def test_single_unresolved_policy_denies_tool_call() -> None:
    """One broken configured policy must DENY TOOL_CALL, not leave an empty ALLOW gate."""
    gate = RunnerToolPolicyGate.from_spec(_agent_with_policies(_broken_policy()))
    assert not gate.is_empty

    verdict = await gate.evaluate_tool_call("web_search", {"q": "x"})
    assert verdict.action == "deny"
    assert verdict.policy_name == "broken"
    assert verdict.deny_text is not None
    assert "failed to resolve" in verdict.deny_text
    assert "ModuleNotFoundError" in verdict.deny_text
    assert _BROKEN_PATH in verdict.deny_text


@pytest.mark.asyncio
async def test_single_unresolved_policy_denies_tool_result() -> None:
    """Unresolved policies must also fail closed on TOOL_RESULT."""
    gate = RunnerToolPolicyGate.from_spec(_agent_with_policies(_broken_policy()))
    output = await gate.evaluate_tool_result("web_search", "raw tool output")
    assert "Denied by policy: broken" in output
    assert "failed to resolve" in output
    assert "raw tool output" not in output


@pytest.mark.asyncio
async def test_unresolved_alongside_valid_still_denies() -> None:
    """A broken policy next to a valid ALLOW policy must still DENY."""
    gate = RunnerToolPolicyGate.from_spec(
        _agent_with_policies(
            _tool_policy("allow_all", _FIXED_ALLOW),
            _broken_policy("broken"),
        ),
    )
    verdict = await gate.evaluate_tool_call("web_search", {})
    assert verdict.action == "deny"
    assert verdict.policy_name == "broken"

    output = await gate.evaluate_tool_result("web_search", "ok")
    assert "Denied by policy: broken" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_phase", [Phase.TOOL_CALL, Phase.TOOL_RESULT])
async def test_unresolved_policy_denies_both_tool_phases(
    configured_phase: Phase,
) -> None:
    """Resolution failure denies both tool phases regardless of its selector."""
    broken = _tool_policy(
        "phase_broken",
        FunctionRef(path=_BROKEN_PATH),
        phases=[configured_phase],
    )
    gate = RunnerToolPolicyGate.from_spec(_agent_with_policies(broken))

    verdict = await gate.evaluate_tool_call("web_search", {})
    assert verdict.action == "deny"
    assert verdict.policy_name == "phase_broken"

    output = await gate.evaluate_tool_result("web_search", "raw tool output")
    assert "Denied by policy: phase_broken" in output
    assert "raw tool output" not in output


@pytest.mark.asyncio
async def test_valid_allow_policy_unchanged() -> None:
    """Successfully resolved ALLOW policies still allow tool call and result."""
    gate = RunnerToolPolicyGate.from_spec(
        _agent_with_policies(_tool_policy("allow_all", _FIXED_ALLOW)),
    )
    verdict = await gate.evaluate_tool_call("web_search", {"q": "ok"})
    assert verdict.action == "allow"
    assert verdict.deny_text is None

    output = await gate.evaluate_tool_result("web_search", "tool output")
    assert output == "tool output"


@pytest.mark.asyncio
async def test_valid_deny_policy_unchanged() -> None:
    """Successfully resolved DENY policies keep their reason and denial shape."""
    gate = RunnerToolPolicyGate.from_spec(
        _agent_with_policies(_tool_policy("block", _FIXED_DENY)),
    )
    verdict = await gate.evaluate_tool_call("web_search", {})
    assert verdict.action == "deny"
    assert verdict.policy_name == "block"
    assert verdict.reason == "blocked by valid policy"
    assert verdict.deny_text is not None
    body = json.loads(verdict.deny_text.split("] ", 1)[1])
    assert body["denied_by_policy"] == "block"
    assert body["reason"] == "blocked by valid policy"


@pytest.mark.asyncio
async def test_resolve_diagnostic_omits_exception_message_secrets() -> None:
    """Deny text must not echo exception strings that may contain secrets."""

    class _SecretError(RuntimeError):
        """Stand-in whose message looks like a leaked credential."""

    secret = "api_key=SUPER_SECRET_TOKEN_XYZ"

    def _boom(_ps: FunctionPolicySpec) -> None:
        raise _SecretError(secret)

    import omnigent.runner.policy as policy_mod

    original = policy_mod.resolve_function_policy
    policy_mod.resolve_function_policy = _boom  # type: ignore[assignment]
    try:
        gate = RunnerToolPolicyGate.from_spec(_agent_with_policies(_broken_policy("leaky")))
    finally:
        policy_mod.resolve_function_policy = original

    verdict = await gate.evaluate_tool_call("web_search", {})
    assert verdict.action == "deny"
    assert verdict.deny_text is not None
    assert secret not in verdict.deny_text
    assert "SUPER_SECRET" not in verdict.deny_text
    assert "_SecretError" in verdict.deny_text
    assert "failed to resolve" in verdict.deny_text


@pytest.mark.asyncio
async def test_resolve_log_omits_exception_message_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Resolution logs retain safe context without exception text or traceback."""
    import omnigent.runner.policy as policy_mod

    secret = "api_key=SUPER_SECRET_LOG_TOKEN_XYZ"

    class _SecretLogError(RuntimeError):
        """Stand-in whose message looks like a leaked log credential."""

    def _boom(_ps: FunctionPolicySpec) -> None:
        raise _SecretLogError(secret)

    original = policy_mod.resolve_function_policy
    policy_mod.resolve_function_policy = _boom  # type: ignore[assignment]
    try:
        with caplog.at_level("ERROR", logger=policy_mod.__name__):
            RunnerToolPolicyGate.from_spec(_agent_with_policies(_broken_policy("leaky_log")))
    finally:
        policy_mod.resolve_function_policy = original

    assert "_SecretLogError" in caplog.text
    assert _BROKEN_PATH in caplog.text
    assert secret not in caplog.text
    assert "SUPER_SECRET" not in caplog.text


@pytest.mark.asyncio
async def test_evaluation_time_exception_still_fails_closed() -> None:
    """A resolved policy that raises at evaluate time remains fail-closed DENY."""
    # arguments force the factory to return a raising evaluator.
    raising = FunctionRef(
        path=_RAISING_PATH,
        arguments={"action": "allow"},
    )
    gate = RunnerToolPolicyGate.from_spec(
        _agent_with_policies(_tool_policy("raises_at_eval", raising)),
    )
    # Swap the underlying callable to raise while keeping the gate non-empty.
    gated = gate._policies[0]
    original = gated.policy._callable

    def _raise(_event: object) -> None:
        raise RuntimeError("eval boom")

    gated.policy._callable = _raise  # type: ignore[method-assign]
    try:
        verdict = await gate.evaluate_tool_call("web_search", {})
    finally:
        gated.policy._callable = original

    assert verdict.action == "deny"
    assert verdict.policy_name == "raises_at_eval"
    assert verdict.deny_text is not None
    assert "policy raised" in verdict.deny_text
    assert "RuntimeError" in verdict.deny_text


@pytest.mark.asyncio
async def test_no_policies_still_allows() -> None:
    """An agent with no guardrails policies keeps the empty-gate ALLOW path."""
    gate = RunnerToolPolicyGate.from_spec(
        AgentSpec(spec_version=1, name="no-policies"),
    )
    assert gate.is_empty
    verdict = await gate.evaluate_tool_call("web_search", {})
    assert verdict.action == "allow"
    output = await gate.evaluate_tool_result("web_search", "ok")
    assert output == "ok"
