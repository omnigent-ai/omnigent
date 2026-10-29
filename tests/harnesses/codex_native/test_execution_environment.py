"""Tests for host-local Codex remote-execution configuration."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native.execution_environment import (
    CodexExecutionMode,
    CodexRemoteExecution,
    CodexRemoteExecutionConfigError,
    apply_execution_selection,
    read_remote_execution,
    resolve_execution_selection,
)


def test_remote_execution_requires_a_local_hook_cwd(tmp_path: Path) -> None:
    """A remote-only cwd cannot strand Codex's host-side command hooks."""
    missing = tmp_path / "shared-cwd"

    with pytest.raises(CodexRemoteExecutionConfigError, match="same absolute path"):
        CodexRemoteExecution(
            exec_server_url="ws://127.0.0.1:43111",
            cwd=str(missing),
        )

    missing.mkdir()
    assert CodexRemoteExecution(
        exec_server_url="ws://127.0.0.1:43111",
        cwd=str(missing),
    ).cwd == str(missing)


@pytest.mark.posix_only
def test_remote_execution_round_trips_privately_and_clears(tmp_path: Path) -> None:
    """One atomic binding survives restart without exposing its URL broadly."""
    bridge_dir = tmp_path / "bridge"
    shared_cwd = tmp_path / "shared-cwd"
    shared_cwd.mkdir()
    binding = CodexRemoteExecution(
        exec_server_url="ws://127.0.0.1:43111",
        cwd=str(shared_cwd),
    )

    apply_execution_selection(bridge_dir, binding)

    assert read_remote_execution(bridge_dir) == binding
    config_path = bridge_dir / "remote_execution.json"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

    apply_execution_selection(bridge_dir, CodexExecutionMode.LOCAL)
    assert read_remote_execution(bridge_dir) is None
    assert not config_path.exists()


def test_missing_remote_execution_preserves_legacy_local_mode(tmp_path: Path) -> None:
    """A session created before this feature remains local without migration."""
    assert read_remote_execution(tmp_path / "missing-bridge") is None
    apply_execution_selection(tmp_path / "missing-bridge", CodexExecutionMode.PRESERVE)
    assert read_remote_execution(tmp_path / "missing-bridge") is None


@pytest.mark.parametrize(
    ("url", "cwd", "local", "message"),
    [
        ("ws://127.0.0.1:43111", None, False, "--exec-cwd"),
        (None, "/home/agent/workspace", False, "--exec-server-url"),
        ("http://127.0.0.1:43111", "/home/agent/workspace", False, "ws:// or wss://"),
        ("ws://127.0.0.1:not-a-port", "/home/agent/workspace", False, "ws:// or wss://"),
        (" ws://127.0.0.1:43111", "/home/agent/workspace", False, "ws:// or wss://"),
        ("ws://127.0.0.1:43111/#fragment", "/home/agent/workspace", False, "ws:// or wss://"),
        ("ws://127.0.0.1:43111", "relative/workspace", False, "absolute"),
        ("ws://127.0.0.1:43111", "/home/agent/workspace", True, "mutually exclusive"),
    ],
)
def test_remote_execution_selection_rejects_partial_or_unsafe_configuration(
    url: str | None,
    cwd: str | None,
    local: bool,
    message: str,
) -> None:
    """Invalid CLI combinations fail before they can alter session state."""
    with pytest.raises(ValueError, match=message):
        resolve_execution_selection(
            exec_server_url=url,
            exec_cwd=cwd,
            local_execution=local,
        )


def test_existing_malformed_remote_execution_fails_closed(tmp_path: Path) -> None:
    """Corrupt state fails closed but an explicit complete selection repairs it."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    config_path = bridge_dir / "remote_execution.json"
    config_path.write_text('{"exec_server_url":', encoding="utf-8")

    with pytest.raises(CodexRemoteExecutionConfigError, match="malformed"):
        read_remote_execution(bridge_dir)

    shared_cwd = tmp_path / "shared-cwd"
    shared_cwd.mkdir()
    replacement = CodexRemoteExecution(
        exec_server_url="ws://127.0.0.1:43111",
        cwd=str(shared_cwd),
    )
    apply_execution_selection(bridge_dir, replacement)
    assert read_remote_execution(bridge_dir) == replacement

    config_path.write_text('{"exec_server_url":', encoding="utf-8")
    apply_execution_selection(bridge_dir, CodexExecutionMode.LOCAL)
    assert read_remote_execution(bridge_dir) is None
