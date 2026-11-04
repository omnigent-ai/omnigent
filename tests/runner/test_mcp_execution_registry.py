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
@pytest.mark.parametrize("ending", ["failed", "session-cancelled"])
async def test_step_observer_returns_none_when_execution_ends_without_result(ending: str) -> None:
    """An observer of a running step gets no result when the step fails or is cancelled."""
    registry = McpExecutionRegistry()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _failing_work() -> McpExecutionResult:
        started.set()
        await release.wait()
        raise RuntimeError("tool failed")

    assert await registry.wait_for_step_result("conv_restart", "mcpop_restart", "initial") is None
    execution = asyncio.create_task(
        registry.execute(
            session_id="conv_restart",
            operation_id="mcpop_restart",
            step="initial",
            params={},
            run=_failing_work,
        )
    )
    await started.wait()
    observer = asyncio.create_task(
        registry.wait_for_step_result("conv_restart", "mcpop_restart", "initial")
    )
    done, _pending = await asyncio.wait({observer}, timeout=0.05)
    assert not done

    if ending == "failed":
        release.set()
    else:
        await registry.cancel_session("conv_restart")

    assert await asyncio.wait_for(observer, timeout=5.0) is None
    with pytest.raises(RuntimeError if ending == "failed" else asyncio.CancelledError):
        await execution


@pytest.mark.asyncio
async def test_cancelled_step_observer_leaves_execution_running() -> None:
    """Cancelling a step observer must not cancel the execution it waits on."""
    registry = McpExecutionRegistry()
    started = asyncio.Event()
    release = asyncio.Event()
    result = McpExecutionResult(status_code=200, content={"result": {"output": "done"}})

    async def _external_work() -> McpExecutionResult:
        started.set()
        await release.wait()
        return result

    execution = asyncio.create_task(
        registry.execute(
            session_id="conv_restart",
            operation_id="mcpop_restart",
            step="initial",
            params={},
            run=_external_work,
        )
    )
    await started.wait()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            registry.wait_for_step_result("conv_restart", "mcpop_restart", "initial"),
            timeout=0.05,
        )

    assert not execution.done()
    release.set()
    assert await asyncio.wait_for(execution, timeout=5.0) == result
    assert (
        await registry.wait_for_step_result("conv_restart", "mcpop_restart", "initial") == result
    )
