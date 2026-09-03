"""Import-graph guards for the runner: the MCP SDK stays out of it.

The ``mcp`` package costs ~300ms to import (``mcp.types`` plus the FastMCP
server graph its ``__init__`` pulls) and is only needed once a session's spec
actually connects to an MCP server. It used to arrive unconditionally, via
``runner/app.py`` -> ``proxy_mcp_manager`` -> ``mcp_manager``, and again at
runtime because ``_entry.main()`` builds a ``RunnerMcpManager`` for every
runner. That put it on the cold-zygote start that ``POST /v1/sessions`` awaits,
and on the whole per-session import cost of any runner the zygote did not fork.

Every assertion here runs in a fresh subprocess, the same way
``tests/runner/test_identity.py`` guards ``runner.identity`` against FastAPI:
the shared pytest session has long since imported ``mcp`` for the MCP tests
themselves, so only a clean interpreter can tell whether the runner graph
pulls it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import omnigent

_OMNIGENT_ROOT = str(Path(omnigent.__file__).resolve().parent)
_ECHO_SERVER = str(
    Path(__file__).resolve().parents[1] / "tools" / "fixtures" / "echo_stdio_mcp_server.py"
)

# Reports the loaded tree plus whether the MCP SDK is resident. The tree line
# is checked against this process's own ``omnigent``, so a probe that resolved a
# different checkout fails loudly instead of silently "passing".
_PREAMBLE = """
import sys, threading

import omnigent
print("TREE", omnigent.__file__)


def mcp_loaded():
    return sorted(m for m in sys.modules if m == "mcp" or m.startswith("mcp."))
"""


def _probe(body: str, timeout: float = 300.0) -> list[str]:
    """Run *body* in a fresh interpreter that resolves this checkout.

    :param body: Probe source appended to the shared preamble.
    :param timeout: Seconds to allow the probe.
    :returns: The probe's stdout lines.
    """
    # Hand the child the same import roots as this process so it resolves
    # ``omnigent`` to the code under test (worktree or installed package) — a
    # bare ``-c`` subprocess otherwise misses pytest's rootdir sys.path entries.
    child_env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    result = subprocess.run(
        [sys.executable, "-c", _PREAMBLE + body],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, f"probe failed:\n{result.stdout}\n{result.stderr}"
    lines = result.stdout.strip().splitlines()
    tree = next(line for line in lines if line.startswith("TREE "))
    assert tree.removeprefix("TREE ").startswith(_OMNIGENT_ROOT), (
        f"probe resolved a different omnigent than the code under test: {tree}"
    )
    return lines


def test_runner_app_import_does_not_pull_the_mcp_sdk() -> None:
    """``omnigent.runner.app`` must import with no ``mcp`` module resident."""
    lines = _probe(
        """
import omnigent.runner.app  # noqa: F401
print("MCP", mcp_loaded())
"""
    )
    assert "MCP []" in lines, (
        f"omnigent.runner.app pulled the MCP SDK into its import graph: {lines}"
    )


def test_building_the_runner_mcp_manager_does_not_pull_the_mcp_sdk() -> None:
    """``_entry.main()`` builds one per runner, so construction must stay free.

    Deferring the import in ``runner/app.py`` alone would have been undone here:
    every runner instantiates ``RunnerMcpManager`` unconditionally at startup.
    """
    lines = _probe(
        """
from omnigent.runner.mcp_manager import RunnerMcpManager
RunnerMcpManager()
print("MCP", mcp_loaded())
"""
    )
    assert "MCP []" in lines, f"constructing RunnerMcpManager imported the MCP SDK: {lines}"


def test_zygote_pre_import_excludes_mcp_and_warms_it_separately() -> None:
    """The zygote's boot-time graph skips MCP; its warm-up adds it, thread-free.

    The zygote's own import sits on the first session-create's critical path, so
    MCP is warmed later from the idle serve loop — which must stay
    single-threaded, since that is the state every runner is forked from.
    """
    lines = _probe(
        """
from omnigent.runner._zygote import _import_mcp_graph, _import_runner_graph

_import_runner_graph()
print("BOOT", mcp_loaded())
_import_mcp_graph()
print("WARMED", bool(mcp_loaded()))
print("THREADS", threading.active_count())
"""
    )
    assert "BOOT []" in lines, f"the zygote pre-imported the MCP SDK: {lines}"
    assert "WARMED True" in lines, f"the zygote warm-up did not import the MCP SDK: {lines}"
    assert "THREADS 1" in lines, f"warming MCP started a thread, breaking fork safety: {lines}"


def test_mcp_declaring_spec_still_works_from_an_mcp_free_runner_graph() -> None:
    """End to end from a clean process: the deferred imports still resolve.

    Boots the runner graph (no MCP resident), then drives a real stdio MCP
    subprocess through ``RunnerMcpManager`` — exercising the lazily imported
    ``McpServerConnection`` — and checks the SDK only arrives at that point.
    """
    lines = _probe(
        f"""
import asyncio, sys

import omnigent.runner.app  # noqa: F401
from omnigent.runner.mcp_manager import RunnerMcpManager
from omnigent.spec.types import AgentSpec, MCPServerConfig

print("BEFORE", mcp_loaded())
spec = AgentSpec(
    spec_version=1,
    mcp_servers=[
        MCPServerConfig(
            name="echo-test",
            transport="stdio",
            command=sys.executable,
            args=[{_ECHO_SERVER!r}],
        )
    ],
)


async def main():
    manager = RunnerMcpManager()
    try:
        result = await manager.schemas_for(spec)
        return sorted(result.tool_names), result.failures
    finally:
        await manager.shutdown()


names, failures = asyncio.run(main())
print("TOOLS", names, failures)
print("AFTER", bool(mcp_loaded()))
"""
    )
    assert "BEFORE []" in lines, f"the runner graph pulled the MCP SDK: {lines}"
    tools = next(line for line in lines if line.startswith("TOOLS "))
    assert "echo-test__echo" in tools, f"MCP-declaring spec lost its tool: {lines}"
    assert "AFTER True" in lines, f"the real MCP connect never imported the SDK: {lines}"
