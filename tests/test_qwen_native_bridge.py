"""Unit tests for qwen-native MCP bridge config wiring."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omnigent import qwen_native_bridge


def test_write_mcp_config_registers_omnigent_relay(tmp_path: Path) -> None:
    """``write_mcp_config`` writes the omnigent server into ``.qwen/settings.json``."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bridge_dir = tmp_path / "bridge"

    path = qwen_native_bridge.write_mcp_config(workspace, bridge_dir)

    assert path == workspace / ".qwen" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    server = data["mcpServers"]["omnigent"]
    # Points at the shared stdio relay implemented in claude_native_bridge.
    assert server["args"][:4] == ["-I", "-m", "omnigent.claude_native_bridge", "serve-mcp"]
    assert str(bridge_dir) in server["args"]
    # trust:true auto-approves qwen's own MCP gate (Omnigent gates separately).
    assert server["trust"] is True
    # The relay's bearer token was written for ``serve-mcp`` to read at startup.
    assert (bridge_dir / "bridge.json").is_file()
    token = json.loads((bridge_dir / "bridge.json").read_text())["token"]
    assert isinstance(token, str) and token


def test_write_mcp_config_preserves_existing_settings(tmp_path: Path) -> None:
    """Merging into an existing settings.json keeps unrelated keys and servers."""
    workspace = tmp_path / "ws"
    settings_dir = workspace / ".qwen"
    settings_dir.mkdir(parents=True)
    settings = settings_dir / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "theme": "dark",
                "mcpServers": {"other": {"command": "x"}},
            }
        ),
        encoding="utf-8",
    )

    qwen_native_bridge.write_mcp_config(workspace, tmp_path / "bridge")

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert set(data["mcpServers"]) == {"other", "omnigent"}


def test_write_mcp_config_recovers_from_malformed_settings(tmp_path: Path) -> None:
    """A malformed settings.json is replaced rather than crashing the launch."""
    workspace = tmp_path / "ws"
    settings_dir = workspace / ".qwen"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text("{ not json", encoding="utf-8")

    qwen_native_bridge.write_mcp_config(workspace, tmp_path / "bridge")

    data = json.loads((settings_dir / "settings.json").read_text(encoding="utf-8"))
    assert "omnigent" in data["mcpServers"]


def test_write_mcp_bridge_config_is_idempotent(tmp_path: Path) -> None:
    """The relay token is generated once and preserved across re-launches."""
    bridge_dir = tmp_path / "bridge"
    qwen_native_bridge.write_mcp_bridge_config(bridge_dir)
    first = (bridge_dir / "bridge.json").read_text()
    qwen_native_bridge.write_mcp_bridge_config(bridge_dir)
    assert (bridge_dir / "bridge.json").read_text() == first


def test_mcp_launch_env_points_at_per_session_approvals(tmp_path: Path) -> None:
    """The launch env isolates the approvals store inside the bridge dir."""
    bridge_dir = tmp_path / "bridge"
    env = qwen_native_bridge.mcp_launch_env(bridge_dir)
    assert env == {"QWEN_CODE_MCP_APPROVALS_PATH": str(bridge_dir / "mcpApprovals.json")}


def test_approve_mcp_server_runs_qwen_with_workspace_cwd_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``approve_mcp_server`` invokes ``qwen mcp approve`` with the launch cwd/env."""
    calls: dict[str, object] = {}

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls["cmd"] = cmd
        calls["cwd"] = kwargs.get("cwd")
        calls["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(qwen_native_bridge.subprocess, "run", _fake_run)
    workspace = tmp_path / "ws"
    bridge_dir = tmp_path / "bridge"

    assert qwen_native_bridge.approve_mcp_server(
        workspace, bridge_dir, qwen_command="/usr/bin/qwen"
    )
    assert calls["cmd"] == ["/usr/bin/qwen", "mcp", "approve", "omnigent"]
    assert calls["cwd"] == str(workspace)
    # The approve must target the SAME isolated store the TUI will read.
    assert isinstance(calls["env"], dict)
    assert calls["env"]["QWEN_CODE_MCP_APPROVALS_PATH"] == str(bridge_dir / "mcpApprovals.json")


def test_approve_mcp_server_returns_false_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero ``qwen`` exit degrades to False (caller falls back to prompt)."""

    def _fail(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(qwen_native_bridge.subprocess, "run", _fail)
    assert not qwen_native_bridge.approve_mcp_server(
        tmp_path / "ws", tmp_path / "bridge", qwen_command="/usr/bin/qwen"
    )


def test_approve_mcp_server_handles_missing_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing qwen binary (OSError) degrades to False, never raising."""

    def _raise(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("no such file")

    monkeypatch.setattr(qwen_native_bridge.subprocess, "run", _raise)
    assert not qwen_native_bridge.approve_mcp_server(
        tmp_path / "ws", tmp_path / "bridge", qwen_command="/nope/qwen"
    )
