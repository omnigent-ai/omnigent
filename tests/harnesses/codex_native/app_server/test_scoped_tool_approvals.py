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
    """Arbitrary MCP servers, non-sys tools, and unknown modes are ignored."""
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
        'mcp_servers.omnigent.tools.sys_os_shell.approval_mode="deny"',
    ]


def test_no_implicit_approvals_when_args_are_empty_or_truncated() -> None:
    """A missing or empty launch set grants nothing."""
    assert codex_mcp_tool_approval_overrides(None) == []
    assert codex_mcp_tool_approval_overrides([]) == []
    assert codex_mcp_tool_approval_overrides(["-c"]) == []
    assert codex_mcp_tool_approval_overrides(["--", "-c", f'{_TOOL}="approve"']) == []


def test_build_carries_launch_approvals_into_the_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app-server config receives the same explicit approvals as the TUI."""
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
        terminal_launch_args=["-c", f'{_TOOL}="approve"'],
    )
    assert f'{_TOOL}="approve"' in server.config_overrides
