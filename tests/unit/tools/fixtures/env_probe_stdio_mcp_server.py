"""Minimal stdio MCP server that reports its own process environment.

Used by :mod:`tests.unit.tools.test_mcp_stdio_e2e` to prove the runner-auth
secret is stripped from the env handed to a spec-author-controlled MCP
subprocess, while a benign ``config.env`` overlay still
reaches it. Exposes a single ``read_env`` tool that returns the value
of a requested variable as seen by *this* subprocess.

Kept deterministic and dependency-free (only ``mcp``) so the e2e test
needs no external services or credentials.

Usage:

    python tests/tools/fixtures/env_probe_stdio_mcp_server.py

``FastMCP.run()`` defaults to stdio transport.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("env-probe")


@mcp.tool()
def read_env(name: str) -> str:
    """
    Report whether env var *name* is visible to this subprocess.

    :param name: Environment variable to read, e.g.
        ``"OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN"``.
    :returns: ``f"set:{value}"`` when present, or ``"<unset>"`` when
        the variable is absent from this process's environment.
    """
    value = os.environ.get(name)
    return "<unset>" if value is None else f"set:{value}"


if __name__ == "__main__":
    mcp.run()
