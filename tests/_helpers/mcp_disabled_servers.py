"""Launch-wire activation, user MCP settings and CLI config layers for executor hooks."""

from pathlib import Path

import pytest
import yaml

from omnigent import mcp_disabled_servers
from omnigent.config import global_config_path
from omnigent.host.connect import _build_runner_env
from omnigent.host.frames import HostLaunchRunnerFrame, decode_host_frame, encode_host_frame
from omnigent.runner.identity import runner_mcp_disabled_servers_active_harnesses


def launch_activation(harnesses: list[str] | None) -> list[str] | None:
    """Send caller-owned activation through the codec and filtered runner environment."""
    frame = decode_host_frame(
        encode_host_frame(
            HostLaunchRunnerFrame(
                "request",
                "token",
                "/repo",
                mcp_disabled_servers_active_harnesses=harnesses,
            )
        )
    )
    assert isinstance(frame, HostLaunchRunnerFrame)
    env = _build_runner_env(
        {},
        server_url="http://server",
        runner_id="runner",
        binding_token="token",
        workspace="/repo",
        parent_pid=1,
        mcp_disabled_servers_active_harnesses=frame.mcp_disabled_servers_active_harnesses,
    )
    return runner_mcp_disabled_servers_active_harnesses(env)


def write_mcp_inheritance(mcp: dict[str, object]) -> Path:
    """Write a caller-owned top-level ``mcp`` setting to the active user config."""
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"mcp": mcp}))
    return path


def isolate_codex_system_layers(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Point Codex's system and managed config layers under *root* instead of ``/etc``."""
    monkeypatch.setattr(mcp_disabled_servers, "CODEX_SYSTEM_CONFIG_PATH", root / "config.toml")
    monkeypatch.setattr(
        mcp_disabled_servers, "CODEX_MANAGED_CONFIG_PATH", root / "managed_config.toml"
    )
    return root


def isolate_claude_settings(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Point Claude's user and managed settings under *root*; return a project directory.

    The returned directory is also the working directory, so launches without an
    explicit cwd read project settings from it rather than from this checkout.
    """
    from omnigent.onboarding import ambient

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root / "claude-config"))
    monkeypatch.setattr(
        ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (root / "managed/managed-settings.json",)
    )
    project = root / "project"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(project)
    return project
