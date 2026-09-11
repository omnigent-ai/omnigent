"""Opt-in live tests for the builtin CodeBuddy ACP harness.

Install CodeBuddy and sign in on the test host, then run::

    OMNIGENT_E2E_CODEBUDDY=1 python -m pytest tests/e2e/test_codebuddy_acp_e2e.py -v

These tests use the CLI's existing account and consume its allowance. Set
OMNIGENT_CODEBUDDY_PATH to select a test installation or wrapper.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
from omnigent.runtime.workflow import _build_acp_cli_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEBUDDY") != "1",
    reason="Requires OMNIGENT_E2E_CODEBUDDY=1 and a signed-in CodeBuddy CLI",
)


@pytest.fixture
async def codebuddy(tmp_path: Path) -> AsyncIterator[AcpExecutor]:
    """Launch the catalog command using the same path override as a real host."""
    spec = AgentSpec(
        spec_version=1,
        name="codebuddy-live-test",
        instructions="Follow the user's test instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codebuddy"}),
    )
    env = _build_acp_cli_spawn_env(spec, harness="codebuddy", cwd=tmp_path)
    executor = AcpExecutor(
        AcpAgentConfig(
            command=env["HARNESS_ACP_COMMAND"],
            name=env["HARNESS_ACP_NAME"],
            omnigent_mcp=env["HARNESS_ACP_OMNIGENT_MCP"] == "1",
        ),
        cwd=str(tmp_path),
    )
    try:
        yield executor
    finally:
        await executor.close()


async def _reply(executor: AcpExecutor, prompt: str, *, tools: list[dict] | None = None) -> str:
    chunks: list[str] = []
    completed = False
    async with asyncio.timeout(120):
        async for event in executor.run_turn(
            [{"role": "user", "content": prompt}],
            tools=tools or [],
            system_prompt="Run only the requested test. Do not commit or push.",
        ):
            if isinstance(event, TextChunk):
                chunks.append(event.text)
            elif isinstance(event, TurnComplete):
                completed = True
            elif isinstance(event, ExecutorError):
                pytest.fail(f"CodeBuddy executor error: {event.message}")
    assert completed, "CodeBuddy did not complete the turn"
    assert chunks, "CodeBuddy did not stream any text"
    return "".join(chunks)


async def test_codebuddy_acp_streams_and_retains_context(codebuddy: AcpExecutor) -> None:
    """Follow-up requests retain context without resending the original marker."""
    marker = "CODEBUDDY_" + secrets.token_hex(8)
    first = await _reply(codebuddy, f"Do not use tools. Remember and reply with exactly {marker}.")
    assert marker in first
    second = await _reply(
        codebuddy, "Do not use tools. Reply with the marker from my last message."
    )
    assert marker in second


async def test_codebuddy_acp_calls_omnigent_mcp_tool(codebuddy: AcpExecutor) -> None:
    """An actual CodeBuddy MCP call reaches the shared Omnigent tool relay."""
    marker = "MCP_" + secrets.token_hex(8)
    calls: list[str] = []
    approvals: list[str] = []

    async def execute_tool(name: str, arguments: dict) -> dict:
        calls.append(name)
        assert name == "codebuddy_probe"
        assert arguments == {}
        return {"marker": marker}

    async def approve_probe(name: str, arguments: dict) -> bool:
        allowed = (
            name in {"codebuddy_probe", "mcp__omnigent__codebuddy_probe"} and arguments == {}
        ) or (
            name == "DeferExecuteTool"
            and arguments.get("toolName") == "mcp__omnigent__codebuddy_probe"
            and arguments.get("params") == {}
        )
        if allowed:
            approvals.append(name)
        return allowed

    codebuddy._tool_executor = execute_tool
    codebuddy._elicitation_handler = approve_probe
    response = await _reply(
        codebuddy,
        "Call the codebuddy_probe tool provided by the omnigent MCP server. "
        "Reply with its returned marker. Do not use filesystem or shell tools.",
        tools=[
            {
                "name": "codebuddy_probe",
                "description": "Return the test marker. No side effects.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }
        ],
    )
    assert calls == ["codebuddy_probe"], f"MCP calls: {calls}; reply: {response}"
    assert approvals, "CodeBuddy did not request approval for the MCP tool"
    assert marker in response
