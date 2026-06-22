"""Unit tests for omnigent.onboarding.ucode_state."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from omnigent.onboarding.ucode_state import (
    UcodeWorkspaceState,
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
                        "refresh_interval_ms": 900000,
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
    assert state.claude_models["opus"] == "databricks-claude-opus-4-7"
    assert state.codex_models == ["databricks-gpt-5-5"]
    assert state.base_urls["claude"] == f"{_WORKSPACE_URL}/ai-gateway/anthropic"
    assert state.base_urls["codex"] == f"{_WORKSPACE_URL}/ai-gateway/codex/v1"
    assert state.available_tools == ["claude", "codex"]
    assert state.agent("claude") is not None
    assert state.agent("claude").auth_command == "printf token"
    assert state.agent("codex") is not None
    assert state.agent("codex").model == "databricks-gpt-5-5"
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
