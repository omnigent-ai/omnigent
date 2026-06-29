"""Unit tests for qwen-native MCP bridge config wiring."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent import qwen_native_bridge


def test_write_mcp_config_writes_into_bridge_dir_not_workspace(tmp_path: Path) -> None:
    """``write_mcp_config`` writes the ``--mcp-config`` file inside the bridge dir."""
    bridge_dir = tmp_path / "bridge"

    path = qwen_native_bridge.write_mcp_config(bridge_dir)

    # The config lives in the bridge dir — never the workspace (no repo pollution).
    assert path == bridge_dir / "mcp_config.json"
    assert path.parent == bridge_dir

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


def test_write_mcp_config_is_valid_for_qwen_mcp_config_flag(tmp_path: Path) -> None:
    """The payload is the ``{"mcpServers": {...}}`` shape qwen's --mcp-config expects."""
    path = qwen_native_bridge.write_mcp_config(tmp_path / "bridge")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"mcpServers"}
    assert set(data["mcpServers"]) == {"omnigent"}


def test_write_mcp_config_path_is_per_session(tmp_path: Path) -> None:
    """Two sessions get independent config files carrying their own bridge dir."""
    bridge_a = tmp_path / "a"
    bridge_b = tmp_path / "b"
    path_a = qwen_native_bridge.write_mcp_config(bridge_a)
    path_b = qwen_native_bridge.write_mcp_config(bridge_b)

    assert path_a != path_b
    args_a = json.loads(path_a.read_text())["mcpServers"]["omnigent"]["args"]
    args_b = json.loads(path_b.read_text())["mcpServers"]["omnigent"]["args"]
    assert str(bridge_a) in args_a
    assert str(bridge_b) in args_b
    # No cross-contamination: A's config never points at B's bridge dir.
    assert str(bridge_b) not in args_a


def test_mcp_config_path_matches_written_path(tmp_path: Path) -> None:
    """``mcp_config_path`` reports the same path ``write_mcp_config`` writes."""
    bridge_dir = tmp_path / "bridge"
    assert qwen_native_bridge.write_mcp_config(bridge_dir) == (
        qwen_native_bridge.mcp_config_path(bridge_dir)
    )


def test_write_mcp_bridge_config_is_idempotent(tmp_path: Path) -> None:
    """The relay token is generated once and preserved across re-launches."""
    bridge_dir = tmp_path / "bridge"
    qwen_native_bridge.write_mcp_bridge_config(bridge_dir)
    first = (bridge_dir / "bridge.json").read_text()
    qwen_native_bridge.write_mcp_bridge_config(bridge_dir)
    assert (bridge_dir / "bridge.json").read_text() == first
