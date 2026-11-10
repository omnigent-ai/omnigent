"""Per-session Omnigent MCP tool approvals reach the app-server."""

import sys
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import app_server as codex_native_app_server
from omnigent.harnesses.codex_native.app_server import codex_mcp_tool_approval_overrides

_TOOL = "mcp_servers.omnigent.tools.sys_session_send.approval_mode"


@pytest.mark.parametrize(
    "args",
    [
        ["-c", f'{_TOOL}="approve"'],
        ["--config", f'{_TOOL}="approve"'],
        ["--config=" + f'{_TOOL}="approve"'],
        ["-c" + f'{_TOOL}="approve"'],
    ],
)
def test_carries_explicit_omnigent_tool_approvals(args: list[str]) -> None:
    """Every Codex launch-argument spelling carries the approval."""
    assert codex_mcp_tool_approval_overrides(args) == [f'{_TOOL}="approve"']


def test_carries_only_recognized_omnigent_sys_tool_approvals() -> None:
    """Other MCP servers, non-sys tools, and modes Codex rejects are ignored."""
    args = [
        "-c",
        'mcp_servers.omnigent.tools.sys_read_inbox.approval_mode="approve"',
        "-c",
        'mcp_servers.omnigent.tools.sys_os_shell.approval_mode="deny"',
        "-c",
        'mcp_servers.omnigent.tools.not_system.approval_mode="approve"',
        "-c",
        'mcp_servers.other.tools.sys_session_send.approval_mode="approve"',
        "-c",
        'mcp_servers.omnigent.tools.sys_session_send.approval_mode="invented"',
        "-c",
        'approval_policy="never"',
        "-c",
        'mcp_servers.omnigent.tools.sys_session_send.command="oops"',
    ]
    assert codex_mcp_tool_approval_overrides(args) == [
        'mcp_servers.omnigent.tools.sys_read_inbox.approval_mode="approve"',
    ]


@pytest.mark.parametrize(
    ("assignments", "expected"),
    [
        (['"approve"', '"auto"'], []),
        (['"approve"', '"invented"'], []),
        (['"approve"', '"deny"'], []),
        (['"prompt"', '"approve"'], [f'{_TOOL}="approve"']),
        (['"approve"', '"prompt"'], [f'{_TOOL}="prompt"']),
    ],
)
def test_later_assignment_of_the_same_key_wins(
    assignments: list[str], expected: list[str]
) -> None:
    """Only the final Codex assignment for a tool decides what is carried."""
    args = [arg for value in assignments for arg in ("-c", f"{_TOOL}={value}")]
    assert codex_mcp_tool_approval_overrides(args) == expected


@pytest.mark.parametrize(
    "parent_assignment",
    [
        'mcp_servers.omnigent.tools.sys_session_send={approval_mode="prompt"}',
        "mcp_servers.omnigent.tools={}",
        'mcp_servers.omnigent={command="python"}',
    ],
)
def test_later_parent_table_assignment_drops_earlier_approvals(parent_assignment: str) -> None:
    """A parent-table override replaces the tool table, so no approval survives it."""
    args = ["-c", f'{_TOOL}="approve"', "-c", parent_assignment]
    assert codex_mcp_tool_approval_overrides(args) == []


def test_no_implicit_approvals_when_args_are_empty_or_truncated() -> None:
    """A missing or empty launch set grants nothing."""
    assert codex_mcp_tool_approval_overrides(None) == []
    assert codex_mcp_tool_approval_overrides([]) == []
    assert codex_mcp_tool_approval_overrides(["-c"]) == []
    assert codex_mcp_tool_approval_overrides(["--", "-c", f'{_TOOL}="approve"']) == []


def test_build_carries_launch_approvals_into_the_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch approvals reach the app-server after generated MCP overrides."""
    monkeypatch.setattr(codex_native_app_server, "_find_codex_cli", lambda: sys.executable)
    server = codex_native_app_server.build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=None,
        profile=None,
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
        extra_config_overrides=['mcp_servers.omnigent.command="python"', f'{_TOOL}="prompt"'],
        terminal_launch_args=["-c", f'{_TOOL}="approve"'],
    )
    assert server.config_overrides == [
        'mcp_servers.omnigent.command="python"',
        f'{_TOOL}="prompt"',
        f'{_TOOL}="approve"',
    ]
