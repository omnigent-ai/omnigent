"""Omnigent tool errors reach the model as MCP errors on the Claude Agent SDK harness.

The SDK builds the wire ``isError`` from the handler's snake_case ``is_error``
only (``CallToolResult(isError=result.get("is_error", False))``). Before the fix
every ``{"error": ...}`` and ``{"blocked": true}`` Omnigent result, every tool
exception and the missing-executor path reached the model unflagged.

These tests are deliberately not skippable. ``claude-agent-sdk`` is a baseline
dependency in ``pyproject.toml``, so it is always installed in CI; importing it
directly (no ``pytest.importorskip``) turns a missing or broken SDK into a
collection error that fails the ``inner-rest`` shard instead of a quiet skip.

Two seams are exercised:

* the public one, ``create_sdk_mcp_server(...)["instance"]`` and its MCP
  ``CallToolRequest`` handler, which produces the ``CallToolResult`` whose
  ``isError`` the CLI receives; and
* the CLI-facing one, ``Query._handle_sdk_mcp_request``, the method the CLI's
  ``mcp_message`` control request reaches. Its internals differ by SDK version
  (inline JSON-RPC conversion in 0.2.121, ``SdkMcpBridge`` in 0.2.173), but the
  method and its signature exist in both, so the test drives the real wire
  path on whichever version is locked. If an SDK upgrade removes it,
  ``test_cli_wire_entry_point_exists`` fails and names what moved.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import claude_agent_sdk
import pytest
from claude_agent_sdk._internal.query import Query
from mcp.types import CallToolRequest, CallToolRequestParams

from omnigent.inner.claude_sdk_executor import _build_mcp_tools
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.os_env import build_os_env_tools

_SCHEMA = {
    "name": "any_tool",
    "description": "",
    "parameters": {"type": "object", "properties": {}},
}

_ERROR_RESULTS = [
    {"error": "boom"},
    {"blocked": True, "reason": "policy"},
    {"error": "Access to '/x' is blocked: path is outside the environment root"},
]
_SUCCESS_RESULTS = [
    {"stdout": "ok", "exit_code": 0},
    {"error": ""},
    {"error": None, "data": [1]},
    {"blocked": False},
]


def _server(executor: Any) -> Any:
    tools = _build_mcp_tools([_SCHEMA], executor)
    return claude_agent_sdk.create_sdk_mcp_server(name="omnigent", tools=tools)["instance"]


async def _call_public(executor: Any) -> dict[str, Any]:
    """Call the tool through the SDK server's public MCP request handler."""
    server = _server(executor)
    handler = server.request_handlers[CallToolRequest]
    request = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name="any_tool", arguments={})
    )
    result = (await handler(request)).root
    return result.model_dump(by_alias=True, mode="json")


async def _call_wire(executor: Any) -> dict[str, Any]:
    """Call the tool the way the CLI does: an ``mcp_message`` routed through ``Query``."""
    query = Query(
        transport=object(),  # type: ignore[arg-type]  # never used by this path
        is_streaming_mode=True,
        sdk_mcp_servers={"omnigent": _server(executor)},
    )
    try:
        init = await query._handle_sdk_mcp_request(
            "omnigent",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "claude-code", "version": "test"},
                },
            },
        )
        assert init is not None and "result" in init, init
        await query._handle_sdk_mcp_request(
            "omnigent", {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        response = await query._handle_sdk_mcp_request(
            "omnigent",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "any_tool", "arguments": {}},
            },
        )
    finally:
        # 0.2.173 and later keep one bridge per server; 0.2.121 has none.
        for bridge in getattr(query, "_sdk_mcp_bridges", {}).values():
            await bridge.aclose()
    assert response is not None and "result" in response, response
    return response["result"]


_CALLS = pytest.mark.parametrize("call", [_call_public, _call_wire], ids=["public", "wire"])


def test_cli_wire_entry_point_exists() -> None:
    assert callable(getattr(Query, "_handle_sdk_mcp_request", None)), (
        "claude_agent_sdk no longer exposes Query._handle_sdk_mcp_request; the CLI's "
        "mcp_message path moved and this file must follow it, not skip."
    )


@_CALLS
@pytest.mark.parametrize("result", _ERROR_RESULTS)
async def test_error_and_blocked_results_are_flagged(call: Any, result: dict[str, Any]) -> None:
    async def executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return result

    out = await call(executor)
    assert out.get("isError") is True


@_CALLS
async def test_tool_exception_is_flagged(call: Any) -> None:
    async def executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    out = await call(executor)
    assert out.get("isError") is True
    assert "boom" in out["content"][0]["text"]


@_CALLS
async def test_missing_executor_is_flagged(call: Any) -> None:
    out = await call(None)
    assert out.get("isError") is True


@_CALLS
@pytest.mark.parametrize("result", _SUCCESS_RESULTS)
async def test_successes_are_not_flagged(call: Any, result: dict[str, Any]) -> None:
    async def executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return result

    out = await call(executor)
    assert out.get("isError") in (False, None)


@_CALLS
@pytest.mark.parametrize(
    ("command", "exit_code", "flagged"),
    [("printf ok", 0, False), ("printf 'no such thing' >&2; exit 3", 3, True)],
    ids=["exit-0", "exit-3"],
)
async def test_real_shell_tool_nonzero_exit_is_flagged(
    call: Any, tmp_path: Path, command: str, exit_code: int, flagged: bool
) -> None:
    """The real ``sys_os_shell`` tool behind the SDK MCP server.

    The shell tool reports a nonzero exit with a truthy top-level ``error``
    (``omnigent.inner.os_env``). ``_build_mcp_tools`` already meant to flag that
    (it set ``isError``), but the SDK reads only ``is_error``, so before this fix
    a failed command reached the model as a success. A zero exit stays unflagged.
    """
    os_env = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert os_env is not None
    shell = {tool.name(): tool for tool in build_os_env_tools(os_env)}["sys_os_shell"]
    ctx = ToolContext(task_id="t", agent_id="a", workspace=tmp_path)

    async def executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        del name, args  # the test tool has no schema, so the command is fixed here
        raw = await asyncio.to_thread(shell.invoke, json.dumps({"command": command}), ctx)
        return json.loads(raw)

    try:
        out = await call(executor)
    finally:
        os_env.close()
    payload = json.loads(out["content"][0]["text"])
    assert payload["exit_code"] == exit_code
    assert bool(payload.get("error")) is flagged
    assert (out.get("isError") is True) is flagged
