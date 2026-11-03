"""Unit tests for omnigent.onboarding.ucode_state."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from omnigent.onboarding.ucode_state import (
    UcodeWorkspaceState,
    gateway_workspace_id,
    read_current_ucode_state,
    read_ucode_state,
    read_ucode_state_for_workspace,
)
from tests._helpers.ucode_state import shared_host_ucode_state, write_ucode_state

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


_SHARED_HOST = "https://shared.example.com"
_SHARED_WORKSPACE_ID = "1965859176160743"


def test_shared_host_state_records_workspace_and_selector_headers(tmp_path: Path) -> None:
    """A shared-host entry parses its workspace id and only the selector headers."""
    payload = shared_host_ucode_state(_SHARED_HOST, _SHARED_WORKSPACE_ID)
    claude = payload["workspaces"][_SHARED_HOST]["agents"]["claude"]  # type: ignore[index]
    claude["headers"] = {
        "x-databricks-workspace-id": _SHARED_WORKSPACE_ID,
        "X-Databricks-Org-Id": "not-a-number",
        "Authorization": "Bearer must-not-forward",
    }
    state_file = write_ucode_state(tmp_path / "state.json", payload)

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        state = read_ucode_state(_SHARED_HOST)

    assert state is not None
    assert state.workspace_host == _SHARED_HOST
    assert state.workspace_id == _SHARED_WORKSPACE_ID
    assert state.agent("claude").headers == {"X-Databricks-Workspace-Id": _SHARED_WORKSPACE_ID}
    assert state.agent("codex").headers == {
        "X-Databricks-Org-Id": _SHARED_WORKSPACE_ID,
        "X-Databricks-Workspace-Id": _SHARED_WORKSPACE_ID,
    }


def test_read_ucode_state_for_workspace_picks_the_profiles_workspace(tmp_path: Path) -> None:
    """The ``?w=`` block wins; a bare-host copy of another workspace is refused."""
    payload = shared_host_ucode_state(_SHARED_HOST, _SHARED_WORKSPACE_ID)
    other = shared_host_ucode_state(_SHARED_HOST, "42")
    payload["workspaces"][f"{_SHARED_HOST}?w=42"] = other["workspaces"][f"{_SHARED_HOST}?w=42"]  # type: ignore[index]
    state_file = write_ucode_state(tmp_path / "state.json", payload)

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        selected = read_ucode_state_for_workspace(_SHARED_HOST + "/", "42")
        copy = read_ucode_state_for_workspace(_SHARED_HOST, _SHARED_WORKSPACE_ID)
        unselected = read_ucode_state_for_workspace(_SHARED_HOST, None)
        mismatched = read_ucode_state_for_workspace(_SHARED_HOST, "7")

    assert selected is not None and selected.workspace_id == "42"
    assert copy is not None and copy.workspace_id == _SHARED_WORKSPACE_ID
    # Without a profile workspace id the bare-host copy is what ucode itself resolves.
    assert unselected is not None and unselected.workspace_id == _SHARED_WORKSPACE_ID
    assert mismatched is None


def test_read_ucode_state_for_workspace_keeps_single_workspace_hosts(tmp_path: Path) -> None:
    """A profile ``workspace_id`` on a single-workspace host still finds its entry."""
    state_file = _write_state(tmp_path, _VALID_STATE)

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        state = read_ucode_state_for_workspace(_WORKSPACE_URL, "123")

    assert state is not None
    assert state.workspace_id is None
    assert (
        gateway_workspace_id(
            state,
            state.agent("claude"),
            host=_WORKSPACE_URL,
            urls=[f"{_WORKSPACE_URL}/ai-gateway/anthropic"],
        )
        is None
    )


def _shared_state(tmp_path: Path, **block_overrides: object) -> UcodeWorkspaceState:
    """Parse a shared-host entry whose bare-host block takes *block_overrides*."""
    payload = shared_host_ucode_state(_SHARED_HOST, _SHARED_WORKSPACE_ID)
    payload["workspaces"][_SHARED_HOST].update(block_overrides)  # type: ignore[index]
    state_file = write_ucode_state(tmp_path / "state.json", payload)
    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        state = read_ucode_state(_SHARED_HOST)
    assert state is not None
    return state


def test_gateway_workspace_id_selects_only_on_the_profile_host(tmp_path: Path) -> None:
    """The selector applies only to HTTPS URLs on the profile host the entry records."""
    state = _shared_state(tmp_path)
    claude = state.agent("claude")
    on_host = [f"{_SHARED_HOST}/ai-gateway/anthropic"]

    assert gateway_workspace_id(state, claude, host=_SHARED_HOST, urls=on_host) == (
        _SHARED_WORKSPACE_ID
    )
    assert (
        gateway_workspace_id(state, claude, host="https://other.example.com", urls=on_host) is None
    )
    assert (
        gateway_workspace_id(
            state, claude, host=_SHARED_HOST, urls=[*on_host, "https://evil.example/ai-gateway"]
        )
        is None
    )
    assert (
        gateway_workspace_id(
            state, claude, host=_SHARED_HOST, urls=["http://shared.example.com/ai-gateway"]
        )
        is None
    )
    assert gateway_workspace_id(state, claude, host=_SHARED_HOST, urls=[]) is None


def test_gateway_workspace_id_refuses_inconsistent_entries(tmp_path: Path) -> None:
    """An entry for another host, or naming another workspace, selects nothing."""
    on_host = [f"{_SHARED_HOST}/ai-gateway/codex/v1"]
    elsewhere = _shared_state(tmp_path, workspace="https://evil.example")
    assert (
        gateway_workspace_id(elsewhere, elsewhere.agent("codex"), host=_SHARED_HOST, urls=on_host)
        is None
    )

    mismatched = _shared_state(tmp_path)
    codex = mismatched.agent("codex")
    codex.headers = {"X-Databricks-Org-Id": "42"}
    assert gateway_workspace_id(mismatched, codex, host=_SHARED_HOST, urls=on_host) is None

    unselected = _shared_state(tmp_path, workspace_id="not-a-number")
    assert unselected.workspace_id is None
    assert (
        gateway_workspace_id(
            unselected, unselected.agent("codex"), host=_SHARED_HOST, urls=on_host
        )
        is None
    )
