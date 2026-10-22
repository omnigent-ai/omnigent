"""Omnigent's orchestration text names every harness it can launch.

Harnesses with deferred MCP tool loading find tools by keyword search over
names and descriptions, and otherwise see only the server ``instructions``.
These tests keep the harness names in that text in step with the registry
(omnigent-ai/omnigent#11001).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES
from omnigent.harness_plugins import NativeCodingAgent, native_agents
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.tools.builtins.agents import SysAgentListTool
from omnigent.tools.builtins.spawn import (
    SysSessionCreateTool,
    SysSessionGetHistoryTool,
    SysSessionGetInfoTool,
    SysSessionSendTool,
)
from omnigent.tools.harness_keywords import CROSS_HARNESS_AGENT_NAMES


def test_keywords_name_every_native_agent() -> None:
    """Each built-in native coding agent's display name is a searchable keyword."""
    missing = [
        a.display_name for a in native_agents() if a.display_name not in CROSS_HARNESS_AGENT_NAMES
    ]
    assert missing == []


def test_keywords_name_every_acp_cli_harness() -> None:
    """Each built-in ACP CLI harness (grok, jcode, ...) is a searchable keyword."""
    lowered = CROSS_HARNESS_AGENT_NAMES.lower()
    assert [key for key in ACP_CLI_HARNESSES if key not in lowered] == []


@pytest.mark.parametrize(
    "tool",
    [SysAgentListTool, SysSessionCreateTool, SysSessionSendTool],
)
def test_launch_tools_carry_harness_keywords(tool: type) -> None:
    """Discover/launch/converse tools match a keyword search for a vendor name."""
    description = tool.description()
    assert CROSS_HARNESS_AGENT_NAMES in description
    assert "Codex" in description


def test_session_create_states_workflow() -> None:
    """sys_session_create sequences the discover -> launch -> converse steps."""
    description = SysSessionCreateTool.description()
    assert description.startswith("Create a child session from an agent")
    workflow = [
        description.index(n) for n in ("sys_agent_list", "sys_session_create", "sys_session_send")
    ]
    assert workflow == sorted(workflow)


@pytest.mark.parametrize("tool", [SysSessionGetHistoryTool, SysSessionGetInfoTool])
def test_monitor_tools_point_back_at_session_create(tool: type) -> None:
    """The monitoring tools say they are how a launched sub-agent is followed."""
    assert "sys_session_create" in tool.description()


def test_mcp_initialize_instructions_describe_capabilities(tmp_path: Path) -> None:
    """``initialize.instructions`` leads with capabilities, ends with availability."""
    result = claude_native_bridge._handle_mcp_request("initialize", {}, {}, tmp_path)
    instructions = result["instructions"]
    assert instructions.startswith("Omnigent orchestrates agents")
    assert CROSS_HARNESS_AGENT_NAMES in instructions
    for tool_name in (
        "sys_agent_list",
        "sys_session_create",
        "sys_session_send",
        "sys_session_get_history",
        "sys_session_get_info",
        "sys_terminal_",
        "sys_os_",
    ):
        assert tool_name in instructions
    # The availability mechanics stay, but as the closing clause.
    assert instructions.index("Availability:") > instructions.index("sys_session_create")
    assert instructions.rstrip().endswith("workspace file and shell access.")


def test_native_agent_description_derived_by_default() -> None:
    """A native agent without an explicit description gets a self-describing one."""
    codex = next(a for a in native_agents() if a.key == "codex")
    assert codex.description is None
    assert codex.resolved_description == (
        "Interactive Codex CLI session on this host, running the codex-native "
        "harness. Launch with sys_session_create to use Codex as a sub-agent."
    )


def test_native_agent_description_override_wins() -> None:
    """An explicit ``description`` (e.g. from a community plugin) is kept verbatim."""
    agent = NativeCodingAgent(
        key="ghost",
        display_name="Ghost",
        agent_name="ghost-native-ui",
        harness="ghost-native",
        wrapper_label="ghost-native-ui",
        terminal_name="ghost",
        description="Custom summary.",
    )
    assert agent.resolved_description == "Custom summary."
