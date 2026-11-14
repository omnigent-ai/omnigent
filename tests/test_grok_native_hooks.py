"""Unit tests for the grok-native hook decision bridge."""

from __future__ import annotations

import pytest

from omnigent.harnesses.grok_native.hooks import (
    HookDecision,
    hook_decision_payload,
    hook_event_session_id,
    hook_event_tool_input,
    hook_event_tool_name,
    verdict_to_hook_decision,
)


@pytest.mark.parametrize(
    "verdict,expected",
    [("ALLOW", "allow"), ("DENY", "deny"), ("ASK", "ask"), ("OTHER", "defer"), ("", "defer")],
)
def test_verdict_maps_to_hook_decision(verdict: str, expected: str) -> None:
    assert verdict_to_hook_decision(verdict).decision == expected


def test_deny_payload_carries_reason_for_model() -> None:
    payload = hook_decision_payload(verdict_to_hook_decision("DENY", reason="no rm -rf"))
    assert payload == {
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "no rm -rf",
        }
    }


def test_additional_context_nests_under_hook_specific_output() -> None:
    payload = hook_decision_payload(
        HookDecision(decision="allow", additional_context="use uv, not pip")
    )
    assert payload["hookSpecificOutput"]["additionalContext"] == "use uv, not pip"


def test_invalid_decision_rejected() -> None:
    with pytest.raises(ValueError, match="unknown hook decision"):
        hook_decision_payload(HookDecision(decision="maybe"))


def test_event_field_extractors() -> None:
    params = {
        "sessionId": "s1",
        "toolName": "bash",
        "toolInput": {"command": "ls"},
    }
    assert hook_event_session_id(params) == "s1"
    assert hook_event_tool_name(params) == "bash"
    assert hook_event_tool_input(params) == {"command": "ls"}
    # snake_case fallbacks + missing-key defaults
    assert hook_event_tool_name({"tool_name": "read"}) == "read"
    assert hook_event_tool_input({}) == {}
    assert hook_event_tool_input({"toolInput": "junk"}) == {}
    assert hook_event_session_id({"session_id": "x"}) == "x"
