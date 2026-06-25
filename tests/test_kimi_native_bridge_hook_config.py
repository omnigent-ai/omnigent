"""Tests for the kimi-native bridge hook-config helpers."""

from __future__ import annotations

from pathlib import Path

from omnigent.kimi_native_bridge import (
    read_active_session_id,
    read_hook_config,
    write_hook_config,
)


def test_write_then_read_hook_config_round_trips(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    write_hook_config(
        bridge_dir,
        server_url="http://127.0.0.1:8787",
        headers={"Authorization": "Bearer tok"},
        session_id="conv_xyz",
    )
    config = read_hook_config(bridge_dir)
    assert config["ap_server_url"] == "http://127.0.0.1:8787"
    assert config["ap_auth_headers"] == {"Authorization": "Bearer tok"}
    assert config["session_id"] == "conv_xyz"
    assert read_active_session_id(bridge_dir) == "conv_xyz"


def test_read_hook_config_absent_is_empty(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    assert read_hook_config(bridge_dir) == {}
    assert read_active_session_id(bridge_dir) is None
