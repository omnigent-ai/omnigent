"""Tests for host-local Codex remote-execution configuration."""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native.execution_environment import (
    CodexExecutionMode,
    CodexRemoteExecution,
    CodexRemoteExecutionConfigError,
    apply_execution_selection,
    codex_remote_environment_requests,
    configure_codex_execution_environments,
    read_remote_execution,
    resolve_execution_selection,
)


def test_remote_execution_cwd_does_not_need_to_exist_on_the_laptop(tmp_path: Path) -> None:
    """Remote and local environment selections can use different path layouts."""
    missing = tmp_path / "shared-cwd"

    assert CodexRemoteExecution(
        exec_server_url="ws://127.0.0.1:43111",
        cwd=str(missing),
    ).cwd == str(missing)


@pytest.mark.posix_only
def test_remote_execution_materializes_private_dual_environment_registry(
    tmp_path: Path,
) -> None:
    """Codex discovers remote as default without suppressing local tools."""
    codex_home = tmp_path / "codex-home"
    binding = CodexRemoteExecution(
        exec_server_url="ws://127.0.0.1:43111",
        cwd="/home/agent/workspace",
    )

    configure_codex_execution_environments(codex_home, binding)

    config_path = codex_home / "environments.toml"
    assert tomllib.loads(config_path.read_text(encoding="utf-8")) == {
        "default": "remote",
        "include_local": True,
        "environments": [
            {"id": "remote", "url": "ws://127.0.0.1:43111"},
        ],
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

    configure_codex_execution_environments(codex_home, None)
    assert not config_path.exists()


def test_remote_turn_selects_remote_first_and_laptop_second(tmp_path: Path) -> None:
    """One turn can use Arclet code tools and laptop-owned memory tools."""
    assert codex_remote_environment_requests(
        CodexRemoteExecution(
            exec_server_url="ws://127.0.0.1:43111",
            cwd="/home/agent/workspace",
        ),
        local_cwd=str(tmp_path),
        local_workspace_roots=[str(tmp_path)],
    ) == [
        {
            "environmentId": "remote",
            "cwd": "/home/agent/workspace",
            "runtimeWorkspaceRoots": ["/home/agent/workspace"],
        },
        {
            "environmentId": "local",
            "cwd": str(tmp_path),
            "runtimeWorkspaceRoots": [str(tmp_path)],
        },
    ]


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
