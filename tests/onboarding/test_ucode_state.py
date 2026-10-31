"""Unit tests for omnigent.onboarding.ucode_state."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.onboarding.ucode_state import (
    UcodeWorkspaceState,
    _parse_agent_state,
    read_current_ucode_state,
    read_ucode_state,
)

_WORKSPACE_URL = "https://example.databricks.com"


def _state_for_workspaces(workspace_urls: Sequence[str]) -> dict:
    """Build a ucode state payload containing *workspace_urls*.

    :param workspace_urls: Workspace URLs to include.
    :returns: Minimal ucode state payload.
    """
    return {
        "state_version": 3,
        "current_workspace": _WORKSPACE_URL,
        "workspaces": dict.fromkeys(
            workspace_urls,
            _VALID_STATE["workspaces"][_WORKSPACE_URL],
        ),
    }


_VALID_STATE = {
    "state_version": 3,
    "current_workspace": _WORKSPACE_URL,
    "workspaces": {
        _WORKSPACE_URL: {
            "fable_enabled": True,
            "claude_models": {
                "opus": "databricks-claude-opus-4-7",
                "sonnet": "databricks-claude-sonnet-4-6",
                "haiku": "databricks-claude-haiku-4-5",
            },
            "codex_models": ["databricks-gpt-5-5"],
            "base_urls": {
                "claude": f"{_WORKSPACE_URL}/ai-gateway/anthropic",
                "codex": f"{_WORKSPACE_URL}/ai-gateway/codex/v1",
            },
            "available_tools": ["claude", "codex"],
            "agents": {
                "claude": {
                    "model": "databricks-claude-opus-4-7",
                    "base_url": f"{_WORKSPACE_URL}/ai-gateway/anthropic",
                    "auth_command": "printf token",
                    "auth_refresh_interval_ms": 900000,
                    "env": {"ANTHROPIC_BASE_URL": f"{_WORKSPACE_URL}/ai-gateway/anthropic"},
                },
                "codex": {
                    "model": "databricks-gpt-5-5",
                    "base_url": f"{_WORKSPACE_URL}/ai-gateway/codex/v1",
                    "auth_command": "printf token",
                    "auth": {
                        "command": "sh",
                        "args": ["-c", "printf token"],
                        "refresh_interval_ms": 60000,
                    },
                },
                "pi": {
                    "model": "databricks-claude-opus-4-7",
                    "base_urls": {
                        "claude": f"{_WORKSPACE_URL}/ai-gateway/anthropic",
                        "openai": f"{_WORKSPACE_URL}/ai-gateway/codex/v1",
                    },
                    "auth_command": "printf token",
                    "auth_refresh_interval_ms": 900000,
                },
            },
        }
    },
}


def _write_state(tmp_path: Path, data: dict) -> Path:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(data))
    return state_file


def test_read_ucode_state_returns_state_when_configured(tmp_path: Path) -> None:
    """Reads the correct workspace entry when state.json is present."""
    state_file = _write_state(tmp_path, _VALID_STATE)

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        state = read_ucode_state(_WORKSPACE_URL)

    assert state is not None
    assert state.fable_enabled is True
    assert state.claude_models["opus"] == "databricks-claude-opus-4-7"
    assert state.codex_models == ["databricks-gpt-5-5"]
    assert state.base_urls["claude"] == f"{_WORKSPACE_URL}/ai-gateway/anthropic"
    assert state.base_urls["codex"] == f"{_WORKSPACE_URL}/ai-gateway/codex/v1"
    assert state.available_tools == ["claude", "codex"]
    assert state.agent("claude") is not None
    assert state.agent("claude").auth_command == "printf token"
    assert state.agent("codex") is not None
    assert state.agent("codex").model == "databricks-gpt-5-5"
    assert state.agent("codex").auth_refresh_interval_ms == 60000
    assert state.agent("pi") is not None
    assert state.agent("pi").base_urls == {
        "claude": f"{_WORKSPACE_URL}/ai-gateway/anthropic",
        "openai": f"{_WORKSPACE_URL}/ai-gateway/codex/v1",
    }


def test_read_ucode_state_trailing_slash_insensitive(tmp_path: Path) -> None:
    """Workspace URL lookup ignores a trailing slash on either side."""
    state_file = _write_state(tmp_path, _VALID_STATE)

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        state = read_ucode_state(_WORKSPACE_URL + "/")

    assert state is not None
    assert state.claude_models["opus"] == "databricks-claude-opus-4-7"


def test_read_ucode_state_refresh_cadence_precedence(tmp_path: Path) -> None:
    """The top-level cadence wins over Codex's nested one; only a positive int is a cadence."""
    agents = {
        "claude": {"auth_refresh_interval_ms": 300000, "auth": {"refresh_interval_ms": 60000}},
        "codex": {"auth": {"refresh_interval_ms": True}},
        "zero": {"auth_refresh_interval_ms": 0},
        "negative": {"auth": {"refresh_interval_ms": -1}},
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"workspaces": {_WORKSPACE_URL: {"agents": agents}}}))

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_path):
        state = read_ucode_state(_WORKSPACE_URL)

    assert state is not None
    assert state.agent("claude").auth_refresh_interval_ms == 300000
    for name in ("codex", "zero", "negative"):
        assert state.agent(name).auth_refresh_interval_ms is None


def test_read_ucode_state_infers_legacy_fable_opt_in(tmp_path: Path) -> None:
    """Older state records signal Fable opt-in through the model mapping."""
    legacy_state = json.loads(json.dumps(_VALID_STATE))
    workspace = legacy_state["workspaces"][_WORKSPACE_URL]
    workspace.pop("fable_enabled")
    workspace["claude_models"]["fable"] = "system.ai.claude-fable-5"
    state_file = _write_state(tmp_path, legacy_state)

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        state = read_ucode_state(_WORKSPACE_URL)

    assert state is not None
    assert state.fable_enabled is True


def test_read_ucode_state_returns_none_when_file_absent(tmp_path: Path) -> None:
    """Returns None when state.json does not exist."""
    with patch("omnigent.onboarding.ucode_state._STATE_PATH", tmp_path / "nonexistent.json"):
        assert read_ucode_state(_WORKSPACE_URL) is None


def test_read_ucode_state_returns_none_for_unknown_workspace(tmp_path: Path) -> None:
    """Returns None when the workspace isn't in state.json."""
    state_file = _write_state(tmp_path, _VALID_STATE)

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        assert read_ucode_state("https://example-other-workspace.cloud.databricks.com") is None


def test_read_current_ucode_state_uses_current_workspace(tmp_path: Path) -> None:
    """Reads the current workspace entry from state.json."""
    state_file = _write_state(tmp_path, _VALID_STATE)

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        state = read_current_ucode_state()

    assert state is not None
    assert state.codex_models == ["databricks-gpt-5-5"]


def test_read_current_ucode_state_accepts_single_workspace_without_current(
    tmp_path: Path,
) -> None:
    """Falls back to a single workspace when current_workspace is absent."""
    state_without_current = {
        key: value for key, value in _VALID_STATE.items() if key != "current_workspace"
    }
    state_file = _write_state(tmp_path, state_without_current)

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        state = read_current_ucode_state()

    assert state is not None
    assert state.claude_models["opus"] == "databricks-claude-opus-4-7"


def test_read_current_ucode_state_returns_none_for_multiple_without_current(
    tmp_path: Path,
) -> None:
    """Returns None when multiple workspaces have no current selection."""
    state_without_current = {
        "state_version": 3,
        "workspaces": {
            _WORKSPACE_URL: _VALID_STATE["workspaces"][_WORKSPACE_URL],
            "https://other.example.databricks.com": {},
        },
    }
    state_file = _write_state(tmp_path, state_without_current)

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        assert read_current_ucode_state() is None


def test_read_ucode_state_accepts_old_state_version_when_keys_match(tmp_path: Path) -> None:
    """Reads known keys without treating state_version as a hard gate."""
    old = {**_VALID_STATE, "state_version": 2}
    state_file = _write_state(tmp_path, old)

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        state = read_ucode_state(_WORKSPACE_URL)

    assert state is not None
    assert state.codex_models == ["databricks-gpt-5-5"]


def test_read_ucode_state_returns_none_for_malformed_json(tmp_path: Path) -> None:
    """Returns None when state.json is not valid JSON."""
    state_file = tmp_path / "state.json"
    state_file.write_text("not json {{{")

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        assert read_ucode_state(_WORKSPACE_URL) is None


def test_workspace_host_property_uses_workspace_url() -> None:
    """workspace_host returns the workspace URL recorded in state."""
    state = UcodeWorkspaceState(
        workspace_url="https://example.cloud.databricks.com",
        claude_models={},
        codex_models=[],
        base_urls={"claude": "https://example.cloud.databricks.com/ai-gateway/anthropic"},
        available_tools=[],
    )
    assert state.workspace_host == "https://example.cloud.databricks.com"


def test_workspace_host_does_not_depend_on_claude_url() -> None:
    """workspace_host remains available when Claude base URL is absent."""
    state = UcodeWorkspaceState(
        workspace_url="https://example.cloud.databricks.com",
        claude_models={},
        codex_models=[],
        base_urls={},
        available_tools=[],
    )
    assert state.workspace_host == "https://example.cloud.databricks.com"


@pytest.mark.skipif(os.name != "posix", reason="uv POSIX archive layout and shell helper")
@pytest.mark.parametrize(
    "archive_mode", [None, 0o644, 0o755], ids=["missing", "not-executable", "working"]
)
@pytest.mark.parametrize("launcher_name", ["ucode", "ug"])
def test_auth_helper_survives_uv_archive_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_mode: int | None, launcher_name: str
) -> None:
    """A generated helper keeps its workspace/profile through cache cleanup."""
    archive_bin = tmp_path / "archive-v0" / "ephemeral" / "bin"
    archive_bin.mkdir(parents=True)
    archived = archive_bin / launcher_name
    if archive_mode is not None:
        archived.write_text("#!/bin/sh\nprintf 'ephemeral-token'\n")
        archived.chmod(archive_mode)
    persistent_bin = tmp_path / "installed tools"
    persistent_bin.mkdir()
    package = tmp_path / "ucode"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "cli.py").write_text(
        "import sys\ndef main():\n    print('\\n'.join(sys.argv[1:]))\n"
    )
    launcher = persistent_bin / launcher_name
    launcher.write_text(
        f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(tmp_path)!r})\n"
        "from ucode.cli import main\nmain()\n"
    )
    launcher.chmod(0o755)
    other_archive_bin = tmp_path / "archive-v0" / "other" / "bin"
    other_archive_bin.mkdir(parents=True)
    other_archived = other_archive_bin / launcher_name
    other_archived.write_text("#!/bin/sh\nexit 1\n")
    other_archived.chmod(0o755)
    symlink_bin = tmp_path / "symlinks"
    symlink_bin.mkdir()
    (symlink_bin / launcher_name).symlink_to(other_archived)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(map(str, (archive_bin, symlink_bin, other_archive_bin, persistent_bin))),
    )
    argv = [
        str(archived),
        "auth-token",
        "--host",
        _WORKSPACE_URL,
        "--profile",
        "gateway profile",
        "--use-pat",
    ]
    state_file = _write_state(
        tmp_path,
        {
            "workspaces": {
                _WORKSPACE_URL: {
                    "agents": {
                        "codex": {
                            "base_url": f"{_WORKSPACE_URL}/ai-gateway/codex/v1",
                            "auth_command": shlex.join(argv),
                            "auth": {"command": str(archived), "args": argv[1:]},
                        }
                    }
                }
            }
        },
    )
    monkeypatch.setattr("omnigent.onboarding.ucode_state._STATE_PATH", state_file)
    original_state = state_file.read_bytes()

    state = read_ucode_state(_WORKSPACE_URL)

    assert state is not None
    agent = state.agent("codex")
    assert agent is not None and agent.auth_command is not None
    expected = argv if archive_mode == 0o755 else [str(launcher), *argv[1:]]
    assert shlex.split(agent.auth_command) == expected
    assert agent.auth == {"command": expected[0], "args": argv[1:]}
    result = subprocess.run(
        ["/bin/sh", "-c", agent.auth_command],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.splitlines() == (
        ["ephemeral-token"] if archive_mode == 0o755 else argv[1:]
    )
    assert state_file.read_bytes() == original_state


@pytest.mark.parametrize(
    "command",
    [
        "/opt/custom/ucode auth-token --host https://example.databricks.com",
        "/tmp/archive-v0/old/bin/other auth-token --host https://example.databricks.com",
        "/tmp/archive-v0/old/bin/ucode auth-token --host https://example.databricks.com | cat",
        "/tmp/archive-v0/old/bin/ucode auth-token --custom-option value",
        "/tmp/archive-v0/old/bin/ucode auth-token --profile $PROFILE",
        "/tmp/archive-v0/old/bin/ucode auth-token --profile ~",
        "/tmp/archive-v0/old/bin/ucode auth-token --profile gateway*",
        "/tmp/archive-v0/old/bin/ucode auth-token --profile #comment",
        "printf token",
    ],
)
def test_auth_helper_preserves_custom_commands(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only generated uv-cache helpers are eligible for launcher replacement."""
    monkeypatch.setenv("PATH", "/opt")
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.shutil.which", lambda name, path: f"/opt/{name}"
    )
    assert _parse_agent_state({"auth_command": command}).auth_command == command


def test_auth_helper_keeps_archive_when_no_persistent_launcher_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing replacement leaves the configured helper and its failure visible."""
    monkeypatch.setenv("PATH", str(tmp_path))
    command = "/tmp/archive-v0/old/bin/ucode auth-token --profile gateway"
    assert _parse_agent_state({"auth_command": command}).auth_command == command


@pytest.mark.skipif(os.name != "posix", reason="uv POSIX archive layout and shell helper")
@pytest.mark.parametrize("launcher_name", ["ucode", "ug"])
def test_auth_helper_does_not_substitute_unrelated_ug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher_name: str
) -> None:
    """The ug executable on PATH can belong to ugrep instead of ucode."""
    archived = tmp_path / "archive-v0" / "old" / "bin" / launcher_name
    launcher = tmp_path / "ug"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    command = shlex.join([str(archived), "auth-token", "--profile", "gateway"])
    auth = {"command": str(archived), "args": ["auth-token", "--profile", "gateway"]}
    agent = _parse_agent_state({"auth_command": command, "auth": auth})
    assert agent.auth_command == command
    assert agent.auth == auth
