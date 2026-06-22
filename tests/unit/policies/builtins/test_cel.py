"""Tests for the cel_policy builtin factory."""

from __future__ import annotations

import pytest

cel = pytest.importorskip("cel_expr_python", reason="cel-expr-python not installed")

from omnigent.policies.builtins.cel import cel_policy  # noqa: E402

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
