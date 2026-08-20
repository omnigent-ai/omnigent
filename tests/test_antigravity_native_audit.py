"""Tests for the post-hoc Antigravity (agy) policy-audit helpers.

Pure unit tests for :mod:`omnigent.antigravity_native_audit` — the
classification/rendering layer of the audit-only governance path. No I/O; the
async POST + interrupt (in the forwarder) are covered separately.
"""

from __future__ import annotations

from typing import Any

from omnigent.antigravity_native_audit import (
    DEGRADE_NOTICE_TEXT,
    HARNESS_NAME,
    audit_verdict_is_violation,
    audit_violation_warning_text,
    build_audit_evaluation_request,
    build_degrade_notice_item,
    build_policy_violation_item,
    step_to_audit_tool_calls,
)

_CID = "8ca97c49-4711-4f1c-a4f5-c8d8e4979687"


def _planner_tool_step(name: str = "run_command", **args: Any) -> dict[str, Any]:
    """
    Build a PLANNER_RESPONSE step with one tool call.

    :param name: Tool name.
    :param args: Tool ``args`` payload (display keys may be included).
    :returns: A PLANNER_RESPONSE step dict.
    """
    return {
        "step_index": 2,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "content": "Running a command.",
        "tool_calls": [{"name": name, "args": args}],
    }


# ── step_to_audit_tool_calls ───────────────────────────────────────────────


def test_planner_tool_step_yields_neutral_record() -> None:
    """A PLANNER_RESPONSE tool call maps to a {tool_name, tool_input} record."""
    step = _planner_tool_step(name="run_command", CommandLine="echo hi")
    records = step_to_audit_tool_calls(step)
    assert records == [{"tool_name": "run_command", "tool_input": {"CommandLine": "echo hi"}}]


def test_display_only_args_are_stripped() -> None:
    """agy's display-only args (toolAction/toolSummary) are dropped from tool_input."""
    step = _planner_tool_step(
        name="list_dir",
        DirectoryPath="/tmp",
        toolAction="Listing",
        toolSummary="List dir",
    )
    records = step_to_audit_tool_calls(step)
    assert records == [{"tool_name": "list_dir", "tool_input": {"DirectoryPath": "/tmp"}}]


def test_non_planner_step_yields_no_tool_calls() -> None:
    """A tool-result step (MODEL but not PLANNER_RESPONSE) is not a fresh tool call."""
    step = {"step_index": 3, "source": "MODEL", "type": "RUN_COMMAND", "content": "output"}
    assert step_to_audit_tool_calls(step) == []


def test_user_step_yields_no_tool_calls() -> None:
    """A user-input step initiates no tools."""
    step = {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "hi"}
    assert step_to_audit_tool_calls(step) == []


def test_tool_call_without_name_is_skipped() -> None:
    """A tool_calls entry with no usable name is dropped."""
    step = {
        "step_index": 2,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "tool_calls": [{"args": {"x": 1}}, {"name": "", "args": {}}],
    }
    assert step_to_audit_tool_calls(step) == []


def test_multiple_tool_calls_preserved_in_order() -> None:
    """Several tool calls in one step are returned in order."""
    step = {
        "step_index": 2,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "tool_calls": [
            {"name": "a", "args": {"k": 1}},
            {"name": "b", "args": {}},
        ],
    }
    records = step_to_audit_tool_calls(step)
    assert [r["tool_name"] for r in records] == ["a", "b"]


# ── build_audit_evaluation_request ─────────────────────────────────────────


def test_audit_request_is_tool_call_phase_with_harness_and_model() -> None:
    """The audit request lands on PHASE_TOOL_CALL and stamps harness + model."""
    request = build_audit_evaluation_request(
        tool_name="run_command",
        tool_input={"CommandLine": "echo hi"},
        model="gemini-2.5-pro",
    )
    assert request is not None
    event = request["event"]
    assert isinstance(event, dict)
    assert event["type"] == "PHASE_TOOL_CALL"
    assert event["data"] == {"name": "run_command", "arguments": {"CommandLine": "echo hi"}}
    assert event["context"]["harness"] == HARNESS_NAME
    assert event["context"]["model"] == "gemini-2.5-pro"


def test_audit_request_omits_model_when_none() -> None:
    """``model=None`` omits context.model but still stamps the harness."""
    request = build_audit_evaluation_request(tool_name="run_command", tool_input={}, model=None)
    assert request is not None
    event = request["event"]
    assert isinstance(event, dict)
    assert "model" not in event["context"]
    assert event["context"]["harness"] == HARNESS_NAME


def test_audit_request_skips_omnigent_mcp_tools() -> None:
    """``mcp__omnigent__*`` tools are relay-enforced; the audit returns None.

    Only the Omnigent MCP tools are double-counted by the relay path, so
    :func:`build_audit_evaluation_request` (delegating to
    ``hook_payload_to_evaluation_request``) skips exactly those.
    """
    request = build_audit_evaluation_request(
        tool_name="mcp__omnigent__sys_call", tool_input={}, model=None
    )
    assert request is None


def test_audit_request_evaluates_connector_mcp_tools() -> None:
    """Connector-native MCP tools (e.g. ``mcp__github__*``) still need the gate.

    Unlike ``mcp__omnigent__*`` (relay-enforced), connector MCP tools are not
    policy-checked elsewhere, so the audit must produce a request for them.
    """
    request = build_audit_evaluation_request(
        tool_name="mcp__github__create_issue", tool_input={}, model="gemini-2.5-pro"
    )
    assert request is not None
    event = request["event"]
    assert isinstance(event, dict)
    assert event["data"]["name"] == "mcp__github__create_issue"
    assert event["context"]["harness"] == HARNESS_NAME


# ── audit_verdict_is_violation / warning text ──────────────────────────────


def test_deny_is_violation() -> None:
    """DENY is a violation."""
    assert audit_verdict_is_violation({"result": "POLICY_ACTION_DENY"}) is True


def test_ask_is_violation_deny_style() -> None:
    """ASK is treated DENY-style (the tool already ran; it cannot be held)."""
    assert audit_verdict_is_violation({"result": "POLICY_ACTION_ASK"}) is True


def test_allow_is_not_violation() -> None:
    """ALLOW is not a violation."""
    assert audit_verdict_is_violation({"result": "POLICY_ACTION_ALLOW"}) is False


def test_unspecified_is_not_violation() -> None:
    """UNSPECIFIED (no matching policy) is not a violation."""
    assert audit_verdict_is_violation({"result": "POLICY_ACTION_UNSPECIFIED"}) is False


def test_warning_text_includes_reason_and_post_hoc_framing() -> None:
    """The warning carries the policy reason and is framed as already-executed."""
    text = audit_violation_warning_text({"result": "POLICY_ACTION_DENY", "reason": "no rm -rf"})
    assert "no rm -rf" in text
    assert "already executed" in text
    assert text.startswith("[Policy violation]")


def test_warning_text_has_fallback_reason() -> None:
    """A verdict with no reason still renders a sensible warning."""
    text = audit_violation_warning_text({"result": "POLICY_ACTION_ASK"})
    assert "[Policy violation]" in text
    assert "already executed" in text


# ── conversation items ─────────────────────────────────────────────────────


def test_policy_violation_item_is_assistant_message() -> None:
    """The violation warning is an assistant message namespaced by step + call + policy."""
    item = build_policy_violation_item(
        conversation_id=_CID, step_index=2, call_ordinal=0, text="warn"
    )
    assert item["item_type"] == "message"
    item_data = item["item_data"]
    assert isinstance(item_data, dict)
    assert item_data["role"] == "assistant"
    assert item_data["content"] == [{"type": "output_text", "text": "warn"}]
    assert item["response_id"] == f"agy_{_CID}_2_0_policy"


def test_policy_violation_items_distinct_per_call_ordinal_in_one_step() -> None:
    """
    Two violations from the SAME step get DISTINCT response ids via the call
    ordinal — keying on step_index alone would collide them onto one id (a single
    PLANNER_RESPONSE step can carry multiple violating tool calls).
    """
    first = build_policy_violation_item(
        conversation_id=_CID, step_index=2, call_ordinal=0, text="warn-0"
    )
    second = build_policy_violation_item(
        conversation_id=_CID, step_index=2, call_ordinal=1, text="warn-1"
    )
    assert first["response_id"] == f"agy_{_CID}_2_0_policy"
    assert second["response_id"] == f"agy_{_CID}_2_1_policy"
    assert first["response_id"] != second["response_id"]


def test_degrade_notice_item_carries_audit_only_text() -> None:
    """The one-time degrade notice states enforcement is audit-only."""
    item = build_degrade_notice_item(conversation_id=_CID)
    assert item["item_type"] == "message"
    item_data = item["item_data"]
    assert isinstance(item_data, dict)
    assert item_data["content"] == [{"type": "output_text", "text": DEGRADE_NOTICE_TEXT}]
    assert "audit-only" in DEGRADE_NOTICE_TEXT
    assert "not blocked" in DEGRADE_NOTICE_TEXT
    assert item["response_id"] == f"agy_{_CID}_audit_notice"
