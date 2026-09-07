"""Tests for runner-owned MCP execution retention across tunnel requests."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.runner.mcp_execution_registry import (
    McpExecutionConflict,
    McpExecutionRegistry,
    McpExecutionResult,
)


@pytest.mark.asyncio
async def test_cancelled_tunnel_waiter_reattaches_without_reexecuting() -> None:
    """Cancelling one waiter must not cancel or duplicate external work."""
    registry = McpExecutionRegistry()
    started = asyncio.Event()
    release = asyncio.Event()
    invocations = 0

    async def _external_work() -> McpExecutionResult:
        nonlocal invocations
        invocations += 1
        started.set()
        await release.wait()
        return McpExecutionResult(status_code=200, content={"result": {"output": "done"}})

    request = {
        "method": "tools/call",
        "params": {"name": "deploy", "arguments": {"environment": "prod"}},
    }
    first_waiter = asyncio.create_task(
        registry.execute(
            session_id="conv_restart",
            operation_id="mcpop_restart",
            step="initial",
            params=request,
            run=_external_work,
        )
    )
    await started.wait()
    first_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_waiter

    assert registry.has_operation("conv_restart", "mcpop_restart")
    second_waiter = asyncio.create_task(
        registry.execute(
            session_id="conv_restart",
            operation_id="mcpop_restart",
            step="initial",
            params=request,
            run=_external_work,
        )
    )
    release.set()

    assert await second_waiter == McpExecutionResult(
        status_code=200,
        content={"result": {"output": "done"}},
    )
    assert invocations == 1


@pytest.mark.asyncio
async def test_reattach_rejects_changed_execution_parameters() -> None:
    """One operation id cannot be reused to execute different external work."""
    registry = McpExecutionRegistry()
    invocations = 0

    async def _external_work() -> McpExecutionResult:
        nonlocal invocations
        invocations += 1
        return McpExecutionResult(status_code=200, content={"result": {"output": "done"}})

    await registry.execute(
        session_id="conv_restart",
        operation_id="mcpop_restart",
        step="initial",
        params={"arguments": {"environment": "prod"}},
        run=_external_work,
    )

    with pytest.raises(McpExecutionConflict, match="parameters changed"):
        await registry.execute(
            session_id="conv_restart",
            operation_id="mcpop_restart",
            step="initial",
            params={"arguments": {"environment": "staging"}},
            run=_external_work,
        )

    assert invocations == 1
