"""Unit tests for qwen-native MCP bridge config wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent import qwen_native_bridge


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bridge dir under a production-shaped qwen root (passes secure validation).

    ``write_mcp_bridge_config`` now hardens the bridge tree via
    ``_ensure_secure_dir``, which requires the dir to live below a known bridge
    root. Mirror the real layout (``<uid-scoped temp>/qwen-native/<digest>``) so
    the owner-only ancestor walk anchors at ``tmp_path``.
    """
    root = tmp_path / "omnigent-test" / "qwen-native"
    monkeypatch.setattr(qwen_native_bridge, "_BRIDGE_ROOT", root)
    return qwen_native_bridge.bridge_dir_for_session_id("sess")


def test_write_mcp_config_writes_into_bridge_dir_not_workspace(bridge_dir: Path) -> None:
    """``write_mcp_config`` writes the ``--mcp-config`` file inside the bridge dir."""
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


def test_write_mcp_config_is_valid_for_qwen_mcp_config_flag(bridge_dir: Path) -> None:
    """The payload is the ``{"mcpServers": {...}}`` shape qwen's --mcp-config expects."""
    path = qwen_native_bridge.write_mcp_config(bridge_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"mcpServers"}
    assert set(data["mcpServers"]) == {"omnigent"}


def test_write_mcp_config_path_is_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sessions get independent config files carrying their own bridge dir."""
    root = tmp_path / "omnigent-test" / "qwen-native"
    monkeypatch.setattr(qwen_native_bridge, "_BRIDGE_ROOT", root)
    bridge_a = qwen_native_bridge.bridge_dir_for_session_id("a")
    bridge_b = qwen_native_bridge.bridge_dir_for_session_id("b")
    path_a = qwen_native_bridge.write_mcp_config(bridge_a)
    path_b = qwen_native_bridge.write_mcp_config(bridge_b)

    assert path_a != path_b
    args_a = json.loads(path_a.read_text())["mcpServers"]["omnigent"]["args"]
    args_b = json.loads(path_b.read_text())["mcpServers"]["omnigent"]["args"]
    assert str(bridge_a) in args_a
    assert str(bridge_b) in args_b
    # No cross-contamination: A's config never points at B's bridge dir.
    assert str(bridge_b) not in args_a


def test_mcp_config_path_matches_written_path(bridge_dir: Path) -> None:
    """``mcp_config_path`` reports the same path ``write_mcp_config`` writes."""
    assert qwen_native_bridge.write_mcp_config(bridge_dir) == (
        qwen_native_bridge.mcp_config_path(bridge_dir)
    )


def test_write_mcp_bridge_config_is_idempotent(bridge_dir: Path) -> None:
    """The relay token is generated once and preserved across re-launches."""
    qwen_native_bridge.write_mcp_bridge_config(bridge_dir)
    first = (bridge_dir / "bridge.json").read_text()
    qwen_native_bridge.write_mcp_bridge_config(bridge_dir)
    assert (bridge_dir / "bridge.json").read_text() == first


def test_write_mcp_bridge_config_rejects_symlinked_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked bridge-tree ancestor is refused — the token is never written.

    bridge.json holds a bearer token, so the dir must pass owner-only ancestor
    validation. If an attacker pre-creates an ancestor as a symlink, writing the
    token must fail loudly rather than land it in attacker-redirectable storage.
    """
    real_root = tmp_path / "omnigent-test"
    qwen_root = real_root / "qwen-native"
    monkeypatch.setattr(qwen_native_bridge, "_BRIDGE_ROOT", qwen_root)
    bridge_dir = qwen_native_bridge.bridge_dir_for_session_id("sess")

    # Redirect an ancestor (the uid-scoped dir) through a symlink.
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    real_root.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(RuntimeError):
        qwen_native_bridge.write_mcp_bridge_config(bridge_dir)
    # No token leaked into the redirected location.
    assert not (elsewhere / "qwen-native").exists()
