"""Security checks shared by native MCP bridge implementations."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent import claude_native_bridge, codex_native_bridge


def test_secure_dir_allows_owned_omnigent_state_root_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user-owned state-root symlink must not break native MCP startup."""
    home = tmp_path / "home"
    state_root = tmp_path / "state" / ".omnigent"
    home.mkdir()
    state_root.mkdir(mode=0o700, parents=True)
    os.chmod(state_root, 0o700)
    (home / ".omnigent").symlink_to(state_root, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", home / ".omnigent" / "codex-native")

    bridge_dir = home / ".omnigent" / "codex-native" / "session"
    claude_native_bridge._ensure_secure_dir(bridge_dir)

    assert bridge_dir.is_dir()
