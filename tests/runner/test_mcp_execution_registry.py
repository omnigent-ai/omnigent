"""Tests for runner-owned MCP execution retention across tunnel requests."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.runner import mcp_execution_registry as mcp_execution_registry_mod
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
async def test_live_operation_lease_survives_result_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live proxy call must retain completed steps until it returns."""
    monkeypatch.setattr(mcp_execution_registry_mod, "_COMPLETED_TTL_S", 0.0)
    monkeypatch.setattr(mcp_execution_registry_mod, "_MAX_COMPLETED", 0)
    registry = McpExecutionRegistry()
    invocations = 0

    async def _external_work() -> McpExecutionResult:
        nonlocal invocations
        invocations += 1
        return McpExecutionResult(status_code=200, content={"result": {"output": "done"}})

    registry.retain_operation("conv_restart", "mcpop_restart")
    assert registry.has_operation("conv_restart", "mcpop_restart")

    request = {"name": "deploy", "arguments": {}}
    first = await registry.execute(
        session_id="conv_restart",
        operation_id="mcpop_restart",
        step="initial",
        params=request,
        run=_external_work,
    )
    second = await registry.execute(
        session_id="conv_restart",
        operation_id="mcpop_restart",
        step="initial",
        params=request,
        run=_external_work,
    )

    assert first == second
    assert invocations == 1

    registry.release_operation("conv_restart", "mcpop_restart")
    assert not registry.has_operation("conv_restart", "mcpop_restart")


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


@pytest.mark.asyncio
async def test_live_operation_tracks_leases_and_unfinished_executions() -> None:
    """A lease or an unfinished execution marks the session as mid tool call."""
    registry = McpExecutionRegistry()
    assert registry.has_live_operation("conv_tool") is False
    assert registry.oldest_live_operation_age_s("conv_tool") is None

    registry.retain_operation("conv_tool", "mcpop_a")
    registry.retain_operation("conv_tool", "mcpop_a")
    assert registry.has_live_operation("conv_tool") is True
    assert registry.has_live_operation("conv_other") is False
    age = registry.oldest_live_operation_age_s("conv_tool")
    assert age is not None and age >= 0.0
    registry.release_operation("conv_tool", "mcpop_a")
    assert registry.has_live_operation("conv_tool") is True
    registry.release_operation("conv_tool", "mcpop_a")
    assert registry.has_live_operation("conv_tool") is False
    assert registry.oldest_live_operation_age_s("conv_tool") is None

    release = asyncio.Event()

    async def _work() -> McpExecutionResult:
        await release.wait()
        return McpExecutionResult(status_code=200, content={})

    waiter = asyncio.create_task(
        registry.execute(
            session_id="conv_tool",
            operation_id="mcpop_b",
            step="initial",
            params={"method": "tools/call"},
            run=_work,
        )
    )
    await asyncio.sleep(0)
    assert registry.has_live_operation("conv_tool") is True
    release.set()
    await waiter
    assert registry.has_live_operation("conv_tool") is False


@pytest.mark.asyncio
async def test_proxy_call_holds_a_lease_for_its_whole_duration() -> None:
    """``ProxyMcpManager.call_tool`` leases the operation until it returns or raises."""
    from omnigent.runner.proxy_mcp_manager import ProxyMcpManager

    registry = McpExecutionRegistry()
    manager = ProxyMcpManager.__new__(ProxyMcpManager)
    manager._execution_registry = registry  # type: ignore[attr-defined]
    manager._session_id = "conv_proxy"  # type: ignore[attr-defined]
    observed: list[bool] = []

    async def _call(tool_name: str, arguments: dict[str, object], operation_id: str) -> str:
        del tool_name, arguments, operation_id
        observed.append(registry.has_live_operation("conv_proxy"))
        raise RuntimeError("tool failed")

    manager._call_tool_with_operation = _call  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await manager.call_tool(None, "github__search", {})
    assert observed == [True]
    assert registry.has_live_operation("conv_proxy") is False
