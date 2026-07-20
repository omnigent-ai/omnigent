"""Shared Omnigent-MCP tool-name helpers for the harness bench."""

from __future__ import annotations

TARGET_OMNIGENT_MCP_TOOL = "sys_session_list"
_TARGET_TOOL_NAMES = frozenset(
    {
        TARGET_OMNIGENT_MCP_TOOL,
        f"mcp__omnigent__{TARGET_OMNIGENT_MCP_TOOL}",
    }
)


def is_target_omnigent_mcp_tool(name: object) -> bool:
    """Return whether *name* is the target relay tool's bare or wire name."""
    return isinstance(name, str) and name in _TARGET_TOOL_NAMES
