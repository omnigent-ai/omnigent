"""Tests for the cel_policy builtin factory."""

from __future__ import annotations

import pytest

pytest.importorskip("celpy", reason="cel-python not installed")

from omnigent.policies.builtins.cel import cel_policy
from omnigent.policies.function import FunctionPolicy
from omnigent.policies.types import EvaluationContext
from omnigent.spec.types import (
    FunctionPolicySpec,
    Phase,
    PhaseSelector,
    PolicyAction,
    StateUpdateAction,
)

# ── Map return: DENY ────────────────────────────────────────────


def test_deny_matching_tool_call() -> None:
    """Expression returning DENY map on tool_call match."""
    evaluate = cel_policy(
        expression=(
            'event.type == "tool_call" && event.data.name == "sys_os_shell"'
            ' ? {"result": "DENY", "reason": "Shell blocked."}'
            ' : {"result": "ALLOW"}'
        ),
    )
    result = evaluate(
        {
            "type": "tool_call",
            "data": {"name": "sys_os_shell", "arguments": {}},
        }
    )
    assert result == {"result": "DENY", "reason": "Shell blocked."}


def test_allow_non_matching_tool_call() -> None:
    """Non-matching tool call returns ALLOW."""
    evaluate = cel_policy(
        expression=(
            'event.type == "tool_call" && event.data.name == "sys_os_shell"'
            ' ? {"result": "DENY", "reason": "Shell blocked."}'
            ' : {"result": "ALLOW"}'
        ),
    )
    result = evaluate(
        {
            "type": "tool_call",
            "data": {"name": "web_search", "arguments": {}},
        }
    )
    assert result == {"result": "ALLOW"}


def test_deny_with_fallback_reason() -> None:
    """Map without reason key uses the factory default."""
    evaluate = cel_policy(
        expression='{"result": "DENY"}',
        reason="Factory default.",
    )
    result = evaluate({"type": "request"})
    assert result == {"result": "DENY", "reason": "Factory default."}


def test_deny_with_custom_reason() -> None:
    """Map with reason key overrides the factory default."""
    evaluate = cel_policy(
        expression='{"result": "DENY", "reason": "Custom."}',
        reason="Factory default.",
    )
    result = evaluate({"type": "request"})
    assert result == {"result": "DENY", "reason": "Custom."}


# ── Map return: ASK ─────────────────────────────────────────────


def test_ask_verdict() -> None:
    """Expression returning ASK parks for user approval."""
    evaluate = cel_policy(
        expression=(
            'event.type == "tool_call"'
            ' ? {"result": "ASK", "reason": "Approve this?"}'
            ' : {"result": "ALLOW"}'
        ),
    )
    result = evaluate({"type": "tool_call", "data": {"name": "x"}})
    assert result == {"result": "ASK", "reason": "Approve this?"}


def test_ask_with_fallback_reason() -> None:
    """ASK without reason in map uses factory default."""
    evaluate = cel_policy(
        expression='{"result": "ASK"}',
        reason="Please approve.",
    )
    result = evaluate({"type": "request"})
    assert result == {"result": "ASK", "reason": "Please approve."}


# ── Map return: ALLOW ───────────────────────────────────────────


def test_allow_explicit() -> None:
    """Explicit ALLOW map passes through without reason."""
    evaluate = cel_policy(expression='{"result": "ALLOW"}')
    result = evaluate({"type": "request"})
    assert result == {"result": "ALLOW"}


def test_state_updates_pass_through_as_plain_python_values() -> None:
    """CEL maps may return canonical state_updates for conversation state."""
    evaluate = cel_policy(
        expression=(
            "{"
            '"result": "ALLOW",'
            '"state_updates": ['
            '{"key": "risk", "action": "increment", "value": 2},'
            '{"key": "last_tool", "action": "set", "value": event.data.name},'
            '{"key": "weight", "action": "set", "value": 1.5},'
            '{"key": "seen_tools", "action": "append", "value": ["shell", true, null]},'
            '{"key": "raw", "action": "set", "value": b"abc"}'
            "]"
            "}"
        )
    )

    result = evaluate({"type": "tool_call", "data": {"name": "sys_os_shell"}})

    assert result == {
        "result": "ALLOW",
        "state_updates": [
            {"key": "risk", "action": "increment", "value": 2},
            {"key": "last_tool", "action": "set", "value": "sys_os_shell"},
            {"key": "weight", "action": "set", "value": 1.5},
            {"key": "seen_tools", "action": "append", "value": ["shell", True, None]},
            {"key": "raw", "action": "set", "value": b"abc"},
        ],
    }


@pytest.mark.asyncio
async def test_state_updates_coerce_through_function_policy() -> None:
    """The policy engine sees CEL state_updates as typed state mutations."""
    spec = FunctionPolicySpec(
        name="cel_state",
        on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)],
    )
    policy = FunctionPolicy(
        spec,
        cel_policy(
            expression=(
                "{"
                '"result": "ALLOW",'
                '"state_updates": ['
                '{"key": "call_count", "action": "increment", "value": 1},'
                '{"key": "last_decision", "action": "set", "value": "allowed"}'
                "]"
                "}"
            )
        ),
    )

    result = await policy.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="sys_os_shell",
            content={"name": "sys_os_shell"},
        ),
        {},
    )

    assert result.action is PolicyAction.ALLOW
    assert result.state_updates is not None
    assert [(u.key, u.action, u.value) for u in result.state_updates] == [
        ("call_count", StateUpdateAction.INCREMENT, 1),
        ("last_decision", StateUpdateAction.SET, "allowed"),
    ]


def test_state_updates_must_be_a_list() -> None:
    """Malformed CEL state_updates reports the authoring error."""
    evaluate = cel_policy(expression='{"result": "ALLOW", "state_updates": "bad"}')

    with pytest.raises(TypeError, match="state_updates must be a list"):
        evaluate({"type": "request"})


# ── Abstain (non-map returns) ───────────────────────────────────


def test_non_map_return_abstains() -> None:
    """Non-map return (e.g. bool, string) abstains."""
    evaluate = cel_policy(expression="true")
    assert evaluate({"type": "request"}) is None


def test_map_without_result_key_abstains() -> None:
    """Map missing the result key abstains."""
    evaluate = cel_policy(expression='{"reason": "no verdict"}')
    assert evaluate({"type": "request"}) is None


# ── CEL features ────────────────────────────────────────────────


def test_string_contains() -> None:
    """CEL string methods work."""
    evaluate = cel_policy(
        expression=(
            'event.type == "request" && event.data.contains("SECRET")'
            ' ? {"result": "DENY", "reason": "Secret detected."}'
            ' : {"result": "ALLOW"}'
        ),
    )
    assert evaluate({"type": "request", "data": "my SECRET key"}) == {
        "result": "DENY",
        "reason": "Secret detected.",
    }
    assert evaluate({"type": "request", "data": "normal"}) == {"result": "ALLOW"}


def test_request_dict_data_projected_to_user_text() -> None:
    """A request-phase ``data`` dict is projected to ``user_content`` for CEL.

    Regression for #2906: the web input gate now passes REQUEST ``data`` as
    ``{"user_content", "attachments"}``. String CEL expressions authored for the
    request phase (e.g. ``event.data.contains(...)``) must keep matching — a raw
    map would fail-open (``.contains`` raises → abstain → ALLOW), silently
    disabling a UI-configured DENY policy.
    """
    evaluate = cel_policy(
        expression=(
            'event.type == "request" && event.data.contains("SECRET")'
            ' ? {"result": "DENY", "reason": "Secret detected."}'
            ' : {"result": "ALLOW"}'
        ),
    )
    # Structured dict shape with the secret in user_content → still DENY.
    assert evaluate(
        {"type": "request", "data": {"user_content": "my SECRET key", "attachments": []}}
    ) == {"result": "DENY", "reason": "Secret detected."}
    # Clean structured dict → ALLOW (not a crash / abstain).
    assert evaluate(
        {"type": "request", "data": {"user_content": "normal", "attachments": []}}
    ) == {"result": "ALLOW"}


def test_in_list() -> None:
    """CEL ``in`` operator works."""
    evaluate = cel_policy(
        expression=(
            'event.type == "tool_call" && event.data.name in ["rm", "drop"]'
            ' ? {"result": "DENY", "reason": "Blocked."}'
            ' : {"result": "ALLOW"}'
        ),
    )
    assert evaluate({"type": "tool_call", "data": {"name": "drop"}}) == {
        "result": "DENY",
        "reason": "Blocked.",
    }
    assert evaluate({"type": "tool_call", "data": {"name": "read"}}) == {
        "result": "ALLOW",
    }


# ── Error handling ──────────────────────────────────────────────


def test_eval_error_returns_none() -> None:
    """CEL eval errors abstain (fail-open)."""
    evaluate = cel_policy(
        expression='event.nonexistent == "x" ? {"result": "DENY"} : {"result": "ALLOW"}'
    )
    assert evaluate({"type": "request", "data": "hello"}) is None


def test_invalid_syntax_raises() -> None:
    """Invalid CEL syntax is rejected at compile time."""
    with pytest.raises(ValueError, match="CEL"):
        cel_policy(expression="event.type ==== bad")


def test_llm_client_stripped_from_cel_event() -> None:
    """llm_client is dropped before json_to_cel; the expression still evaluates."""

    class _FakeLLMClient:
        pass

    evaluate = cel_policy(expression='{"result": "DENY"}')
    # The engine injects llm_client (a live object) into every real event.
    # CEL expressions cannot use it and json_to_cel cannot convert it, so it
    # is stripped before marshalling. The expression must evaluate normally.
    result = evaluate({"type": "request", "llm_client": _FakeLLMClient()})  # type: ignore[typeddict-unknown-key]
    assert result == {"result": "DENY", "reason": "Denied by policy."}
