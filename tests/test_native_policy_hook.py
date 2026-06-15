"""Unit tests for the shared native-harness policy hook converters."""

from __future__ import annotations

import pytest

from omnigent.native_policy_hook import (
    evaluation_response_to_hook_output,
    hook_payload_to_evaluation_request,
)


def test_pre_tool_use_maps_to_phase_tool_call() -> None:
    """
    A PreToolUse payload becomes a PHASE_TOOL_CALL EvaluationRequest.

    The tool name and arguments must land in ``event.data`` so the
    server's policy engine can match on them. A failure here means the
    server would evaluate an empty/garbled tool call and likely ALLOW
    everything.
    """
    result = hook_payload_to_evaluation_request(
        "PreToolUse",
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
    )
    assert result is not None
    event = result["event"]
    assert event["type"] == "PHASE_TOOL_CALL"
    # The command must survive into args, or the policy can't inspect it.
    assert event["data"] == {"name": "Bash", "arguments": {"command": "rm -rf /"}}


def test_post_tool_use_maps_to_phase_tool_result() -> None:
    """
    A PostToolUse payload becomes a PHASE_TOOL_RESULT EvaluationRequest.

    The result text goes in ``event.data.result`` and the originating
    tool name/args ride along in ``request_data`` so a TOOL_RESULT
    policy can correlate output to the call that produced it. A failure
    means output-inspection policies would see no result or no tool.
    """
    result = hook_payload_to_evaluation_request(
        "PostToolUse",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "cat /etc/passwd"},
            "tool_output": "root:x:0:0:...",
        },
    )
    assert result is not None
    event = result["event"]
    assert event["type"] == "PHASE_TOOL_RESULT"
    assert event["data"]["result"] == "root:x:0:0:..."
    # request_data carries the originating call so result policies can
    # correlate output back to the tool + args that produced it.
    assert event["request_data"] == {
        "name": "Bash",
        "arguments": {"command": "cat /etc/passwd"},
    }


@pytest.mark.parametrize("hook_event", ["PreToolUse", "PostToolUse"])
def test_omnigent_mcp_tools_are_skipped(hook_event: str) -> None:
    """
    Omnigent MCP tools return None and are never sent to /policies/evaluate.

    Omnigent MCP tool calls are already policy-checked by the relay path
    (ProxyMcpManager → Omnigent /mcp endpoint → _evaluate_tool_call_policy).
    If this guard regressed, every MCP tool call would be evaluated
    twice — once via the relay, once via this hook.
    """
    result = hook_payload_to_evaluation_request(
        hook_event,
        {"tool_name": "mcp__omnigent__list_comments", "tool_input": {}, "tool_output": "x"},
    )
    # None signals the caller to skip the POST entirely.
    assert result is None


@pytest.mark.parametrize(
    "hook_event,expected_type",
    [("PreToolUse", "PHASE_TOOL_CALL"), ("PostToolUse", "PHASE_TOOL_RESULT")],
)
def test_connector_native_mcp_tools_are_evaluated(hook_event: str, expected_type: str) -> None:
    """
    Connector-native MCP tools must not be skipped by the native pre-call hook.

    Tools such as ``mcp__github__*`` are injected by the connector layer and
    do not round-trip through Omnigent's MCP proxy, so this hook is their
    TOOL_CALL/TOOL_RESULT policy enforcement site.
    """
    result = hook_payload_to_evaluation_request(
        hook_event,
        {
            "tool_name": "mcp__github__create_issue",
            "tool_input": {"title": "blocked?"},
            "tool_output": "created",
        },
    )
    assert result is not None
    event = result["event"]
    assert event["type"] == expected_type
    if hook_event == "PreToolUse":
        assert event["data"] == {
            "name": "mcp__github__create_issue",
            "arguments": {"title": "blocked?"},
        }
    else:
        assert event["request_data"] == {
            "name": "mcp__github__create_issue",
            "arguments": {"title": "blocked?"},
        }


def test_unknown_hook_event_returns_none() -> None:
    """
    A non-tool hook event (e.g. SessionStart) is not policy-relevant.

    Returning None makes the hook a no-op for events that carry no tool
    call. A failure (returning a request) would POST garbage to the
    server for every lifecycle event.
    """
    assert hook_payload_to_evaluation_request("SessionStart", {"tool_name": "Bash"}) is None


@pytest.mark.parametrize(
    "action,expected_decision",
    [
        ("POLICY_ACTION_DENY", "deny"),
        ("POLICY_ACTION_ASK", "deny"),
    ],
)
def test_pre_tool_use_response_maps_action_to_permission_decision(
    action: str, expected_decision: str
) -> None:
    """
    A constraining proto action maps to the matching permissionDecision.

    DENY→deny. ASK→deny too: ASK is resolved server-side now (URL-based
    elicitation — ``POST /policies/evaluate`` holds the gate and returns
    a hard ALLOW/DENY), so the hook should never see ASK; if it does, it
    must fail closed with ``deny`` rather than the old ``defer`` (which
    handed control to a possibly-permissive harness permission_mode,
    re-opening the bypass). ALLOW is deliberately NOT here — it returns
    None (see test_pre_tool_use_allow_returns_none). A wrong mapping here
    would, e.g., let a DENY verdict run the tool, defeating enforcement.
    """
    output = evaluation_response_to_hook_output("PreToolUse", {"result": action})
    assert output is not None
    hook_specific = output["hookSpecificOutput"]
    assert hook_specific["hookEventName"] == "PreToolUse"
    assert hook_specific["permissionDecision"] == expected_decision


def test_pre_tool_use_allow_returns_none() -> None:
    """
    A PreToolUse ALLOW yields no opinion (None), not ``"allow"``.

    ALLOW is the policy engine's default verdict when no policy matches a
    tool call. Emitting ``permissionDecision: "allow"`` would auto-approve
    the tool in the native harness, suppressing its own permission prompt
    — and, for Claude Code, the ``PermissionRequest`` hook that routes
    that prompt to the web UI. Returning None keeps the policy gate and
    the user's own consent gate independent: the policy layer may block
    (DENY) or demand approval (ASK), but must never silence the harness's
    native prompt. Regression guard for "claude-native elicitations stop
    showing in the web UI" once a PreToolUse policy hook was wired in.
    """
    output = evaluation_response_to_hook_output("PreToolUse", {"result": "POLICY_ACTION_ALLOW"})
    assert output is None


def test_pre_tool_use_deny_includes_reason() -> None:
    """
    A DENY verdict surfaces the policy reason as permissionDecisionReason.

    The reason is what the user/agent sees explaining the block. A
    failure (missing reason) would block tools with no explanation.
    """
    output = evaluation_response_to_hook_output(
        "PreToolUse",
        {"result": "POLICY_ACTION_DENY", "reason": "rm blocked by admin policy"},
    )
    assert output is not None
    hook_specific = output["hookSpecificOutput"]
    assert hook_specific["permissionDecision"] == "deny"
    assert hook_specific["permissionDecisionReason"] == "rm blocked by admin policy"


def test_pre_tool_use_unknown_action_returns_none() -> None:
    """
    An unrecognized/unspecified action yields no opinion (None).

    POLICY_ACTION_UNSPECIFIED (e.g. no agent / no policies) must not be
    coerced into allow or deny — returning None lets the harness apply
    its own default. A failure would fabricate a verdict from no policy.
    """
    output = evaluation_response_to_hook_output(
        "PreToolUse", {"result": "POLICY_ACTION_UNSPECIFIED"}
    )
    assert output is None


def test_post_tool_use_deny_maps_to_additional_context() -> None:
    """
    A PostToolUse DENY becomes an additionalContext warning, not a block.

    PostToolUse fires after the tool ran, so it cannot block — the
    verdict is surfaced to the model as context. A failure would either
    drop the warning or wrongly attempt to block an already-run tool.
    """
    output = evaluation_response_to_hook_output(
        "PostToolUse",
        {"result": "POLICY_ACTION_DENY", "reason": "Sensitive data in output"},
    )
    assert output is not None
    hook_specific = output["hookSpecificOutput"]
    assert hook_specific["hookEventName"] == "PostToolUse"
    # The warning text must carry the reason so the model sees why.
    assert hook_specific["additionalContext"] == "[Policy violation] Sensitive data in output"


def test_post_tool_use_allow_returns_none() -> None:
    """
    A PostToolUse ALLOW produces no output (nothing to inject).

    Only DENY warrants an additionalContext warning. A failure
    (emitting output on ALLOW) would spam the model with empty context
    on every successful tool result.
    """
    output = evaluation_response_to_hook_output("PostToolUse", {"result": "POLICY_ACTION_ALLOW"})
    assert output is None
