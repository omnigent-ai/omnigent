"""Runner ``sys_os_shell`` dispatch under transient and persistent faults.

Each case drives :func:`omnigent.runner.tool_dispatch.execute_tool` through
the real ``ProxyMcpManager`` or ``CallerProcessOSEnvironment`` with the fault
injected at the frame the production tracebacks name: an HTTP 500 from the
server MCP proxy, or a fork ``EAGAIN`` from the helper ``Popen``. One fault
must be absorbed without a dispatch ERROR log; a persistent one must reach
the model as a structured tool error together with that log.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import httpx
import pytest

from omnigent.inner import os_env as os_env_mod
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner import proxy_mcp_manager as proxy_mcp_manager_mod
from omnigent.runner.mcp_execution_registry import MCP_OPERATION_ID_PARAM
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.spec.types import AgentSpec

_TOOL = "sys_os_shell"
_SHELL_ARGS = json.dumps({"command": "echo hi"})
_PROXY_ERROR_LOG = "tool sys_os_shell failed"
_OS_ENV_ERROR_LOG = "runner OSEnvironment dispatch failed for sys_os_shell"


def _is_helper_argv(args: object) -> bool:
    """Only the OS-environment helper spawn takes the injected fork failure."""
    return isinstance(args, (list, tuple)) and "omnigent.inner.os_env" in args and "helper" in args


def _rpc_shell_success(request_body: bytes) -> httpx.Response:
    """Build the MCP proxy's successful ``tools/call`` response.

    :param request_body: The raw JSON-RPC request, echoed for its ``id``.
    :returns: A 200 response whose result carries one text block.
    """
    rpc_id = json.loads(request_body).get("id", 1)
    return httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {"content": [{"type": "text", "text": "hi"}], "isError": False},
            }
        ).encode(),
    )


def _unsandboxed_spec(cwd: Path) -> AgentSpec:
    """Pin the os_env to an unsandboxed helper so the spawn is identical on any host.

    The default os_env is the platform sandbox (``linux_bwrap`` on Linux), which
    refuses to start without the ``bwrap`` binary before ``Popen`` is reached.
    """
    return AgentSpec(
        spec_version=1,
        name="shell-dispatch-fault-recovery",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )


async def test_shell_proxy_transient_500_recovers(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One 500 from the MCP proxy is retried and the dispatch result is the tool output.

    The re-post keeps the operation id, takes a fresh JSON-RPC id, and emits no
    dispatch ERROR log.
    """
    monkeypatch.setattr(proxy_mcp_manager_mod, "_TRANSIENT_PROXY_BACKOFF_S", 0.0)
    session_id = "conv_proxy_transient_500"
    requests: list[dict[str, object]] = []

    def _flaky_proxy(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{session_id}/mcp"
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(500, text="Internal Server Error")
        return _rpc_shell_success(request.content)

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_flaky_proxy),
        base_url="https://omnigent-server.invalid",
    )
    proxy = ProxyMcpManager(session_id, server_client)
    spec = AgentSpec(spec_version=1, name="shell-dispatch-fault-recovery")

    try:
        with caplog.at_level(logging.ERROR, logger="omnigent.runner.tool_dispatch"):
            output = await execute_tool(
                tool_name=_TOOL,
                arguments=_SHELL_ARGS,
                agent_spec=spec,
                conversation_id=session_id,
                mcp_manager=proxy,
            )
    finally:
        await server_client.aclose()

    assert output == "hi", "The retried call must return the successful tool result"
    assert _PROXY_ERROR_LOG not in caplog.text, "A recovered blip must not log a dispatch ERROR"
    assert len(requests) == 2, "Exactly one retry must follow the transient 500"
    # Replay guard: both attempts must carry the same runner-owned operation
    # id (distinct JSON-RPC ids), so the server dedupes instead of re-running.
    first_params = requests[0]["params"]
    second_params = requests[1]["params"]
    assert isinstance(first_params, dict) and isinstance(second_params, dict)
    assert first_params[MCP_OPERATION_ID_PARAM] == second_params[MCP_OPERATION_ID_PARAM]
    assert requests[0]["id"] != requests[1]["id"]


async def test_shell_proxy_persistent_500_surfaces_as_tool_error(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 that never clears becomes a model-facing tool error after the bounded retries.

    Dispatch returns ``Error: RuntimeError: MCP proxy call failed ...`` instead of
    raising, and logs ``tool sys_os_shell failed``.
    """
    monkeypatch.setattr(proxy_mcp_manager_mod, "_TRANSIENT_PROXY_BACKOFF_S", 0.0)
    session_id = "conv_proxy_persistent_500"
    attempts = 0

    def _always_500(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        assert request.url.path == f"/v1/sessions/{session_id}/mcp"
        attempts += 1
        return httpx.Response(500, text="Internal Server Error")

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_always_500),
        base_url="https://omnigent-server.invalid",
    )
    proxy = ProxyMcpManager(session_id, server_client)
    spec = AgentSpec(spec_version=1, name="shell-dispatch-fault-recovery")

    try:
        with caplog.at_level(logging.ERROR, logger="omnigent.runner.tool_dispatch"):
            output = await execute_tool(
                tool_name=_TOOL,
                arguments=_SHELL_ARGS,
                agent_spec=spec,
                conversation_id=session_id,
                mcp_manager=proxy,
            )
    finally:
        await server_client.aclose()

    # The failure is surfaced to the LLM as a tool result, not raised.
    assert output.startswith("Error: RuntimeError: MCP proxy call failed for tool")
    assert f"'{_TOOL}'" in output
    assert f"session '{session_id}'" in output
    assert "500 Internal Server Error" in output
    assert attempts == proxy_mcp_manager_mod._TRANSIENT_PROXY_MAX_RETRIES + 1
    # The runner logged the attributable dispatch failure.
    assert _PROXY_ERROR_LOG in caplog.text


async def test_shell_helper_transient_fork_failure_recovers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One fork ``EAGAIN`` on the helper spawn is retried and the shell command runs.

    The runner-local path (``mcp_manager=None``) returns the structured shell result
    with no dispatch ERROR log; the unsandboxed spec keeps the spawn host-independent.
    """
    monkeypatch.setattr(os_env_mod, "_SPAWN_TRANSIENT_BACKOFF_S", 0.0)
    real_popen = subprocess.Popen
    spawn_attempts = 0

    def _fork_blocked_once(args: object, *popen_args: object, **popen_kwargs: object) -> object:
        nonlocal spawn_attempts
        if not _is_helper_argv(args):
            return real_popen(args, *popen_args, **popen_kwargs)  # type: ignore[arg-type]
        spawn_attempts += 1
        if spawn_attempts == 1:
            raise BlockingIOError(35, "Resource temporarily unavailable")
        return real_popen(args, *popen_args, **popen_kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os_env_mod.subprocess, "Popen", _fork_blocked_once)

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.tool_dispatch"):
        output = await execute_tool(
            tool_name=_TOOL,
            arguments=_SHELL_ARGS,
            agent_spec=_unsandboxed_spec(tmp_path),
            conversation_id="conv_fork_transient",
            mcp_manager=None,
        )

    payload = json.loads(output)
    assert isinstance(payload, dict)
    assert "error" not in payload, f"A recovered spawn must not surface an error: {payload}"
    assert payload["stdout"].strip() == "hi"
    assert payload["exit_code"] == 0
    assert spawn_attempts == 2, "Exactly one retry must follow the transient fork failure"
    assert _OS_ENV_ERROR_LOG not in caplog.text


async def test_shell_helper_persistent_fork_failure_surfaces_as_tool_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent fork ``EAGAIN`` becomes a structured tool error after the bounded attempts.

    Dispatch returns ``{"error": "[Errno 35] ..."}`` instead of raising and logs
    ``runner OSEnvironment dispatch failed for sys_os_shell``.
    """
    monkeypatch.setattr(os_env_mod, "_SPAWN_TRANSIENT_BACKOFF_S", 0.0)
    real_popen = subprocess.Popen
    spawn_attempts = 0

    def _fork_blocked(args: object, *popen_args: object, **popen_kwargs: object) -> object:
        nonlocal spawn_attempts
        if not _is_helper_argv(args):
            return real_popen(args, *popen_args, **popen_kwargs)  # type: ignore[arg-type]
        spawn_attempts += 1
        raise BlockingIOError(35, "Resource temporarily unavailable")

    monkeypatch.setattr(os_env_mod.subprocess, "Popen", _fork_blocked)

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.tool_dispatch"):
        output = await execute_tool(
            tool_name=_TOOL,
            arguments=_SHELL_ARGS,
            agent_spec=_unsandboxed_spec(tmp_path),
            conversation_id="conv_fork_persistent",
            mcp_manager=None,
        )

    # Structured error result, not a raised exception.
    payload = json.loads(output)
    assert isinstance(payload, dict)
    assert "Resource temporarily unavailable" in payload["error"]
    assert "Errno 35" in payload["error"]
    assert spawn_attempts == os_env_mod._SPAWN_TRANSIENT_ATTEMPTS
    # The runner logged the attributable dispatch failure.
    assert _OS_ENV_ERROR_LOG in caplog.text
