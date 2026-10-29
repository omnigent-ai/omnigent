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
from omnigent.runtime.requester import (
    REQUESTER_FIELD,
    ExecutionRequester,
    InputRequester,
    current_tool_requester,
)
from omnigent.util.json_types import JsonObject


@pytest.mark.asyncio
async def test_cancelled_tunnel_waiter_reattaches_without_reexecuting() -> None:
    """Cancelling one waiter must not cancel or duplicate external work."""
    registry = McpExecutionRegistry()
    started = asyncio.Event()
    release = asyncio.Event()
    invocations = 0
    requester = ExecutionRequester(
        InputRequester(user_id="pat@example.test", account_generation="v1")
    ).snapshot()

    async def _external_work() -> McpExecutionResult:
        nonlocal invocations
        invocations += 1
        started.set()
        assert current_tool_requester.get() == requester
        await release.wait()
        assert current_tool_requester.get() == requester
        return McpExecutionResult(status_code=200, content={"result": {"output": "done"}})

    request: JsonObject = {
        "method": "tools/call",
        "params": {
            "name": "deploy",
            "arguments": {"environment": "prod"},
            REQUESTER_FIELD: requester.model_dump(),
        },
    }
    token = current_tool_requester.set(requester)
    try:
        first_waiter = asyncio.create_task(
            registry.execute(
                session_id="conv_restart",
                operation_id="mcpop_restart",
                step="initial",
                params=request,
                run=_external_work,
            )
        )
    finally:
        current_tool_requester.reset(token)
    await started.wait()
    first_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_waiter

    assert registry.has_operation("conv_restart", "mcpop_restart")
    fresh_owner = ExecutionRequester(
        InputRequester(user_id="bryan@example.test", account_generation="v1")
    ).snapshot()
    token = current_tool_requester.set(fresh_owner)
    try:
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
        assert current_tool_requester.get() == fresh_owner
    finally:
        current_tool_requester.reset(token)
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
@pytest.mark.parametrize(
    "changed",
    ["arguments", "execution_id", "user_id", "account_generation", "input_id", "missing"],
)
async def test_reattach_rejects_changed_execution_parameters(changed: str) -> None:
    """One operation id cannot be reused to execute different external work."""
    registry = McpExecutionRegistry()
    invocations = 0
    requester = ExecutionRequester(
        InputRequester(user_id="pat@example.test", account_generation="v1")
    ).snapshot()
    params: JsonObject = {
        "arguments": {"environment": "prod"},
        REQUESTER_FIELD: requester.model_dump(),
    }
    changed_params: JsonObject = {**params}
    if changed == "arguments":
        changed_params["arguments"] = {"environment": "staging"}
    elif changed == "missing":
        changed_params.pop(REQUESTER_FIELD)
    else:
        changed_requester = requester.model_dump()
        if changed == "execution_id":
            changed_requester[changed] = "f" * 32
        else:
            changed_requester["origin"][changed] = {
                "user_id": "bryan@example.test",
                "account_generation": "v2",
                "input_id": "f" * 32,
            }[changed]
        changed_params[REQUESTER_FIELD] = changed_requester

    async def _external_work() -> McpExecutionResult:
        nonlocal invocations
        invocations += 1
        return McpExecutionResult(status_code=200, content={"result": {"output": "done"}})

    await registry.execute(
        session_id="conv_restart",
        operation_id="mcpop_restart",
        step="initial",
        params={"method": "tools/call", "params": params},
        run=_external_work,
    )

    with pytest.raises(McpExecutionConflict, match="parameters changed"):
        await registry.execute(
            session_id="conv_restart",
            operation_id="mcpop_restart",
            step="initial",
            params={"method": "tools/call", "params": changed_params},
            run=_external_work,
        )

    assert invocations == 1
