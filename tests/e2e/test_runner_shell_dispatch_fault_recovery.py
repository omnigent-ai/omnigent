"""E2E regression: runner ``sys_os_shell`` dispatch under upstream/host faults.

An agent runs a shell command while a dependency misbehaves, in one of two
ways that production logs record as tool-dispatch failures:

* The Omnigent server's MCP proxy (``POST /v1/sessions/{id}/mcp``) answers
  HTTP 500 while forwarding the ``tools/call``. Such server-side errors are
  routinely transient (a restart, a load-balancer blip, momentary overload).
* The runner-local OSEnvironment path cannot fork the sandbox helper:
  ``subprocess.Popen`` raises ``BlockingIOError`` (``EAGAIN``) because the
  host is briefly out of process capacity.

Correct dispatch behavior has two halves, and this module asserts both:

* **Transient faults must be absorbed.** A single 500 or one fork ``EAGAIN``
  must not permanently fail the tool call; the runner retries (replay-safe:
  the MCP proxy retry re-posts under the same retained operation id, and a
  failed spawn has no side effects) and the command succeeds.
* **Persistent faults must surface.** When the fault does not clear within
  the bounded retries, the failure must come back to the LLM as a structured
  tool-result error — never be swallowed, never raise out of dispatch — and
  the runner must emit its attributable ERROR log
  (``tool sys_os_shell failed`` / ``runner OSEnvironment dispatch failed for
  sys_os_shell`` at ``omnigent.runner.tool_dispatch``).

Each fault is injected at the exact frame the production tracebacks name
(the proxy's HTTP response; ``os_env`` helper ``Popen``), and every test
drives the real dispatch entrypoint
:func:`omnigent.runner.tool_dispatch.execute_tool` end-to-end through the
real ``ProxyMcpManager`` / ``CallerProcessOSEnvironment``.

This is a backend (``api``-surface) behavior: the trigger is not a user
action and the observable is the tool-result string plus the ERROR log line.

Run with::

    .venv/bin/python -m pytest tests/e2e/test_runner_shell_dispatch_fault_recovery.py -v
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


async def test_shell_proxy_transient_500_recovers(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One 500 from the server MCP proxy must be retried, not fail the call.

    The proxy answers 500 on the first ``tools/call`` post and succeeds on
    the second — the transient shape of the production failure. The runner
    must re-post under the same operation id (so the server can reattach any
    work already started instead of replaying it) and return the successful
    result, without emitting the dispatch ERROR log.

    **What breaks if wrong:** a single server blip during a shell tool call
    permanently fails the agent's command.
    """
    # ``raising=False``: on a tree without the retry the constant is absent;
    # the test must then fail on the behavior below, not on this setattr.
    monkeypatch.setattr(proxy_mcp_manager_mod, "_TRANSIENT_PROXY_BACKOFF_S", 0.0, raising=False)
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
    """A 500 that never clears must surface as a tool error after bounded retries.

    Drives ``execute_tool`` with a real :class:`ProxyMcpManager` whose server
    client answers every ``tools/call`` with HTTP 500. The runner must:

    * stop after the bounded retry budget (never loop forever),
    * return the failure to the LLM as an ``Error: RuntimeError: MCP proxy
      call failed for tool 'sys_os_shell' ...500...`` tool result (never
      swallow it or raise out of dispatch), and
    * emit the ``tool sys_os_shell failed`` ERROR log at
      ``omnigent.runner.tool_dispatch``.

    **What breaks if wrong:** a persistent server-side failure is silently
    dropped, crashes the turn, or retries without bound.
    """
    monkeypatch.setattr(proxy_mcp_manager_mod, "_TRANSIENT_PROXY_BACKOFF_S", 0.0, raising=False)
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
    # Bounded retries: the initial post plus two retries, then give up.
    assert attempts == 3
    # The runner logged the attributable dispatch failure.
    assert _PROXY_ERROR_LOG in caplog.text


async def test_shell_helper_transient_fork_failure_recovers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One fork ``EAGAIN`` while spawning the helper must be retried, not fail.

    Drives ``execute_tool`` down the runner-local OSEnvironment path
    (``mcp_manager=None``) with ``subprocess.Popen`` raising
    ``BlockingIOError`` on the first spawn only — the transient shape of host
    fork pressure. The helper never started, so the retry is side-effect
    free; the shell command must then run and succeed. The os_env is pinned
    to an unsandboxed spec so the retried helper spawns identically on any
    host (an active sandbox backend would need this checkout mounted).

    **What breaks if wrong:** momentary host fork pressure permanently fails
    the agent's shell command.
    """
    monkeypatch.setattr(os_env_mod, "_SPAWN_TRANSIENT_BACKOFF_S", 0.0, raising=False)
    real_popen = subprocess.Popen
    spawn_attempts = 0

    def _fork_blocked_once(*args: object, **kwargs: object) -> object:
        nonlocal spawn_attempts
        spawn_attempts += 1
        if spawn_attempts == 1:
            raise BlockingIOError(35, "Resource temporarily unavailable")
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os_env_mod.subprocess, "Popen", _fork_blocked_once)
    spec = AgentSpec(
        spec_version=1,
        name="shell-dispatch-fault-recovery",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.tool_dispatch"):
        output = await execute_tool(
            tool_name=_TOOL,
            arguments=_SHELL_ARGS,
            agent_spec=spec,
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
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fork ``EAGAIN`` that never clears must surface as a structured tool error.

    Drives ``execute_tool`` down the runner-local OSEnvironment path with
    every ``subprocess.Popen`` raising ``BlockingIOError(35)`` (the host stays
    out of fork capacity). The runner must:

    * stop after the bounded spawn attempts (never loop forever),
    * return a structured ``{"error": "[Errno 35] Resource temporarily
      unavailable"}`` tool result (never raise out of dispatch), and
    * emit the ``runner OSEnvironment dispatch failed for sys_os_shell``
      ERROR log at ``omnigent.runner.tool_dispatch``.

    **What breaks if wrong:** persistent host resource exhaustion crashes the
    turn or is dropped instead of being surfaced as an attributable error.
    """
    monkeypatch.setattr(os_env_mod, "_SPAWN_TRANSIENT_BACKOFF_S", 0.0, raising=False)
    spawn_attempts = 0

    def _fork_blocked(*_args: object, **_kwargs: object) -> object:
        nonlocal spawn_attempts
        spawn_attempts += 1
        raise BlockingIOError(35, "Resource temporarily unavailable")

    monkeypatch.setattr(os_env_mod.subprocess, "Popen", _fork_blocked)

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.tool_dispatch"):
        output = await execute_tool(
            tool_name=_TOOL,
            arguments=_SHELL_ARGS,
            agent_spec=None,
            conversation_id="conv_fork_persistent",
            mcp_manager=None,
        )

    # Structured error result, not a raised exception.
    payload = json.loads(output)
    assert isinstance(payload, dict)
    assert "Resource temporarily unavailable" in payload["error"]
    assert "Errno 35" in payload["error"]
    # Bounded spawn attempts: the initial spawn plus two retries, then give up.
    assert spawn_attempts == 3
    # The runner logged the attributable dispatch failure.
    assert _OS_ENV_ERROR_LOG in caplog.text
