"""Tests for the prompt_policy builtin factory."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.policies.builtins.prompt import prompt_policy


def _make_event(
    *,
    llm_response: dict[str, Any] | None = None,
    llm_error: Exception | None = None,
    phase: str = "request",
    data: Any = "hello",
) -> dict[str, Any]:
    """Build a policy event with a mock llm_client."""
    mock_response = type("Response", (), {"output_text": json.dumps(llm_response)})()
    client = AsyncMock()
    if llm_error:
        client.create.side_effect = llm_error
    else:
        client.create.return_value = mock_response
    return {
        "type": phase,
        "target": None,
        "data": data,
        "context": {},
        "session_state": {},
        "llm_client": client,
    }


@pytest.mark.asyncio
async def test_allow_verdict() -> None:
    """LLM returns allow → policy returns ALLOW."""
    evaluate = prompt_policy(prompt="Allow everything.")
    event = _make_event(llm_response={"action": "allow", "reason": ""})
    result = await evaluate(event)
    assert result == {"result": "ALLOW"}


@pytest.mark.asyncio
async def test_deny_verdict_with_llm_reason() -> None:
    """LLM returns deny with a reason → policy returns DENY + reason."""
    evaluate = prompt_policy(prompt="Deny Canada.")
    event = _make_event(llm_response={"action": "deny", "reason": "mentions Canada"})
    result = await evaluate(event)
    assert result == {"result": "DENY", "reason": "mentions Canada"}


@pytest.mark.asyncio
async def test_ask_verdict() -> None:
    """LLM returns ask → policy returns ASK."""
    evaluate = prompt_policy(prompt="Ask on tool calls.")
    event = _make_event(llm_response={"action": "ask", "reason": "Approve?"})
    result = await evaluate(event)
    assert result == {"result": "ASK", "reason": "Approve?"}


@pytest.mark.asyncio
async def test_fixed_reason_overrides_llm() -> None:
    """Factory reason= overrides the LLM's reason."""
    evaluate = prompt_policy(prompt="Deny.", reason="Fixed reason.")
    event = _make_event(llm_response={"action": "deny", "reason": "LLM reason"})
    result = await evaluate(event)
    assert result == {"result": "DENY", "reason": "Fixed reason."}


@pytest.mark.asyncio
async def test_llm_error_fails_closed() -> None:
    """LLM call failure → fail-closed DENY."""
    evaluate = prompt_policy(prompt="Test.")
    event = _make_event(llm_error=RuntimeError("LLM down"))
    result = await evaluate(event)
    assert result is not None
    assert result["result"] == "DENY"
    assert "fail-closed" in result["reason"]


@pytest.mark.asyncio
async def test_empty_response_abstains() -> None:
    """Empty LLM response → abstain (None)."""
    evaluate = prompt_policy(prompt="Test.")
    client = AsyncMock()
    client.create.return_value = type("R", (), {"output_text": ""})()
    event = {
        "type": "request",
        "target": None,
        "data": "hello",
        "context": {},
        "session_state": {},
        "llm_client": client,
    }
    result = await evaluate(event)
    assert result is None


@pytest.mark.asyncio
async def test_no_llm_client_abstains() -> None:
    """No llm_client → abstain (None)."""
    evaluate = prompt_policy(prompt="Test.")
    event = {"type": "request", "data": "hello", "llm_client": None}
    result = await evaluate(event)
    assert result is None


@pytest.mark.asyncio
async def test_invalid_action_denies() -> None:
    """LLM returns invalid action → DENY."""
    evaluate = prompt_policy(prompt="Test.")
    event = _make_event(llm_response={"action": "maybe", "reason": ""})
    result = await evaluate(event)
    assert result is not None
    assert result["result"] == "DENY"


@pytest.mark.asyncio
async def test_code_fence_stripped() -> None:
    """LLM wraps JSON in code fences → still parsed correctly."""
    evaluate = prompt_policy(prompt="Test.")
    fenced = '```json\n{"action": "deny", "reason": "fenced"}\n```'
    client = AsyncMock()
    client.create.return_value = type("R", (), {"output_text": fenced})()
    event = {
        "type": "request",
        "target": None,
        "data": "hello",
        "context": {},
        "session_state": {},
        "llm_client": client,
    }
    result = await evaluate(event)
    assert result == {"result": "DENY", "reason": "fenced"}


@pytest.mark.asyncio
async def test_tool_call_event_includes_tool_in_prompt() -> None:
    """Tool call events include the tool name in the classifier prompt."""
    evaluate = prompt_policy(prompt="Block shell.")
    client = AsyncMock()
    client.create.return_value = type(
        "R", (), {"output_text": json.dumps({"action": "allow", "reason": ""})}
    )()
    event = {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {"command": "ls"}},
        "context": {},
        "session_state": {},
        "llm_client": client,
    }
    await evaluate(event)
    # Verify the prompt sent to the LLM mentions the tool
    call_args = client.create.call_args
    prompt_text = call_args.kwargs["input"][0]["content"][0]["text"]
    assert "sys_os_shell" in prompt_text
    assert "tool_call" in prompt_text
