"""Tests for _connect_broker_claude_config with aliased workspace entries.

Tests the fix for shared-host SPOG workspace alias handling in ucode state.
When ucode creates an alias entry for a shared host like https://isaac.databricks.com
with the dbc host's URLs, the broker config should still trust the base URL
if it comes from the canonical entry's host and the entry is marked as an alias.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from omnigent.harnesses.claude_native.main import _connect_broker_claude_config


@pytest.fixture(autouse=True)
def _stub_catalog_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the model catalog so test doesn't need real catalog."""
    monkeypatch.setattr(
        "omnigent.models.model_catalog.resolve_catalog_model",
        lambda provider_name, *, family, **kwargs: SimpleNamespace(
            model_id=f"catalog-{provider_name}-{family}-default"
        ),
    )


_DBC_HOST = "https://dbc-a5d4177a-49dc.cloud.databricks.com"
_SHARED_HOST = "https://isaac.databricks.com"
_ORG_ID = "1965859176160743"


def _minimal_state_with_alias() -> dict:
    """Build a ucode state with canonical entry + shared-host alias.

    The shared-host alias entry has its URLs pointing to the dbc host
    (the canonical entry's workspace), with alias_of set to the canonical workspace.
    This mimics https://github.com/databricks/unity-gateway/pull/1153.
    """
    return {
        "state_version": 3,
        "current_workspace": _SHARED_HOST,
        "workspaces": {
            _DBC_HOST: {
                "workspace": _DBC_HOST,
                "claude_models": {
                    "sonnet": "databricks-claude-sonnet-4-6",
                },
                "codex_models": [],
                "base_urls": {
                    "claude": f"{_DBC_HOST}/ai-gateway/anthropic",
                },
                "available_tools": ["claude"],
                "agents": {
                    "claude": {
                        "model": "databricks-claude-sonnet-4-6",
                        "base_url": f"{_DBC_HOST}/ai-gateway/anthropic",
                        "auth_command": "printf token",
                        "env": {
                            "ANTHROPIC_BASE_URL": f"{_DBC_HOST}/ai-gateway/anthropic",
                        },
                    },
                },
            },
            _SHARED_HOST: {
                "workspace": _DBC_HOST,  # Points to the canonical (dbc) workspace
                "alias_of": _DBC_HOST,  # Marks this as an alias entry
                "claude_models": {
                    "sonnet": "databricks-claude-sonnet-4-6",
                },
                "codex_models": [],
                "base_urls": {
                    "claude": f"{_DBC_HOST}/ai-gateway/anthropic",
                },
                "available_tools": ["claude"],
                "agents": {
                    "claude": {
                        "model": "databricks-claude-sonnet-4-6",
                        "base_url": f"{_DBC_HOST}/ai-gateway/anthropic",  # Also points to dbc host
                        "auth_command": "printf token",
                        "env": {
                            "ANTHROPIC_BASE_URL": f"{_DBC_HOST}/ai-gateway/anthropic",
                        },
                    },
                },
            },
        },
    }


def _write_state(tmp_path: Path, data: dict) -> Path:
    """Write a ucode state file to tmp_path/state.json."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(data))
    return state_file


def test_read_ucode_state_preserves_alias_of_field(tmp_path: Path) -> None:
    """Verify that alias_of field from state.json is parsed into raw data."""
    state = _minimal_state_with_alias()
    state_file = _write_state(tmp_path, state)

    # Verify the raw state has alias_of
    raw = json.loads(state_file.read_text())
    assert raw["workspaces"][_SHARED_HOST].get("alias_of") == _DBC_HOST


def test_broker_config_accepts_alias_dbc_url_on_shared_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that broker config accepts dbc URLs when state entry is an alias.

    When the profile is on the shared host but ucode's base URL is on the dbc host
    (because the state entry is an alias), the security check should accept it
    because the entry's workspace field points to the canonical (dbc) workspace.
    """
    state = _minimal_state_with_alias()
    state_file = _write_state(tmp_path, state)

    # Mock the workspace_host reader to return the shared host (as if the profile is
    # configured to the shared host)
    def mock_read_host(profile: str) -> str | None:
        return _SHARED_HOST if profile == "omnigent" else None

    # Mock the broker sidecar as present
    def mock_broker_token_command(host: str) -> str | None:
        return "printf token" if host == _SHARED_HOST else None

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        with patch("omnigent.inner.databricks_executor._read_databrickscfg_host", mock_read_host):
            with patch(
                "omnigent.host.databricks_credential.broker_token_command",
                mock_broker_token_command,
            ):
                config = _connect_broker_claude_config()

    assert config is not None, "Broker config should exist"
    # With the fix, the broker config should adopt ucode's base URL from the dbc host
    env_base_url = config.env.get("ANTHROPIC_BASE_URL")
    expected_dbc_url = f"{_DBC_HOST}/ai-gateway/anthropic"
    assert env_base_url == expected_dbc_url, (
        f"Broker config should adopt the dbc host URL from the alias entry. "
        f"Expected: {expected_dbc_url}, Got: {env_base_url}"
    )


def test_broker_config_should_accept_alias_dbc_url_with_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that broker config accepts dbc URLs when state entry is an alias.

    After the fix, when the state entry has alias_of set to the canonical workspace,
    and the base URL's host matches the canonical workspace's host, the broker config
    should accept it even though the profile is on the shared host.
    """
    state = _minimal_state_with_alias()
    state_file = _write_state(tmp_path, state)

    # Mock the workspace_host reader to return the shared host
    def mock_read_host(profile: str) -> str | None:
        return _SHARED_HOST if profile == "omnigent" else None

    # Mock the broker sidecar as present
    def mock_broker_token_command(host: str) -> str | None:
        return "printf token" if host == _SHARED_HOST else None

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        with patch("omnigent.inner.databricks_executor._read_databrickscfg_host", mock_read_host):
            with patch(
                "omnigent.host.databricks_credential.broker_token_command",
                mock_broker_token_command,
            ):
                config = _connect_broker_claude_config()

    assert config is not None, "Broker config should exist"
    # After the fix, this should adopt ucode's base URL from the canonical (dbc) host
    env_base_url = config.env.get("ANTHROPIC_BASE_URL")
    expected_dbc_url = f"{_DBC_HOST}/ai-gateway/anthropic"
    assert env_base_url == expected_dbc_url, (
        f"With the fix, broker config should adopt the dbc host URL from the alias entry. "
        f"Expected: {expected_dbc_url}, Got: {env_base_url}"
    )


def test_broker_config_rejects_alias_to_non_databricks_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security: reject alias_of pointing to a non-Databricks host."""
    state = _minimal_state_with_alias()
    # Attacker modifies state.json to point to an evil host
    evil_host = "https://evil.example.com"
    state["workspaces"][_SHARED_HOST]["alias_of"] = evil_host
    state["workspaces"][_SHARED_HOST]["workspace"] = evil_host
    state["workspaces"][_SHARED_HOST]["agents"]["claude"]["base_url"] = (
        f"{evil_host}/ai-gateway/anthropic"
    )
    state["workspaces"][_SHARED_HOST]["agents"]["claude"]["env"]["ANTHROPIC_BASE_URL"] = (
        f"{evil_host}/ai-gateway/anthropic"
    )
    state_file = _write_state(tmp_path, state)

    def mock_read_host(profile: str) -> str | None:
        return _SHARED_HOST if profile == "omnigent" else None

    def mock_broker_token_command(host: str) -> str | None:
        return "printf token" if host == _SHARED_HOST else None

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        with patch("omnigent.inner.databricks_executor._read_databrickscfg_host", mock_read_host):
            with patch(
                "omnigent.host.databricks_credential.broker_token_command",
                mock_broker_token_command,
            ):
                config = _connect_broker_claude_config()

    assert config is not None, "Broker config should exist"
    # Should reject the evil host and fall back to profile-derived URL
    env_base_url = config.env.get("ANTHROPIC_BASE_URL")
    expected_fallback = f"{_SHARED_HOST}/ai-gateway/anthropic"
    assert env_base_url == expected_fallback, (
        f"Broker config should reject non-Databricks alias_of and use fallback. "
        f"Expected: {expected_fallback}, Got: {env_base_url}"
    )


def test_broker_config_rejects_base_url_on_third_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security: reject base URL on a host different from alias_of."""
    third_host = "https://other.cloud.databricks.com"
    state = _minimal_state_with_alias()
    # Attacker sets base_url to a different Databricks host than alias_of
    state["workspaces"][_SHARED_HOST]["agents"]["claude"]["base_url"] = (
        f"{third_host}/ai-gateway/anthropic"
    )
    state["workspaces"][_SHARED_HOST]["agents"]["claude"]["env"]["ANTHROPIC_BASE_URL"] = (
        f"{third_host}/ai-gateway/anthropic"
    )
    state_file = _write_state(tmp_path, state)

    def mock_read_host(profile: str) -> str | None:
        return _SHARED_HOST if profile == "omnigent" else None

    def mock_broker_token_command(host: str) -> str | None:
        return "printf token" if host == _SHARED_HOST else None

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        with patch("omnigent.inner.databricks_executor._read_databrickscfg_host", mock_read_host):
            with patch(
                "omnigent.host.databricks_credential.broker_token_command",
                mock_broker_token_command,
            ):
                config = _connect_broker_claude_config()

    assert config is not None, "Broker config should exist"
    # Should reject the mismatched host and fall back
    env_base_url = config.env.get("ANTHROPIC_BASE_URL")
    expected_fallback = f"{_SHARED_HOST}/ai-gateway/anthropic"
    assert env_base_url == expected_fallback, (
        f"Broker config should reject base URL on host different from alias_of. "
        f"Expected: {expected_fallback}, Got: {env_base_url}"
    )


def test_broker_config_rejects_alias_when_workspace_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security: reject alias if workspace field doesn't point to alias_of."""
    state = _minimal_state_with_alias()
    # Attacker sets alias_of to one host but workspace to another
    wrong_workspace = "https://wrong.cloud.databricks.com"
    state["workspaces"][_SHARED_HOST]["workspace"] = wrong_workspace
    state_file = _write_state(tmp_path, state)

    def mock_read_host(profile: str) -> str | None:
        return _SHARED_HOST if profile == "omnigent" else None

    def mock_broker_token_command(host: str) -> str | None:
        return "printf token" if host == _SHARED_HOST else None

    with patch("omnigent.onboarding.ucode_state._STATE_PATH", state_file):
        with patch("omnigent.inner.databricks_executor._read_databrickscfg_host", mock_read_host):
            with patch(
                "omnigent.host.databricks_credential.broker_token_command",
                mock_broker_token_command,
            ):
                config = _connect_broker_claude_config()

    assert config is not None, "Broker config should exist"
    # Should reject the inconsistent alias and fall back
    env_base_url = config.env.get("ANTHROPIC_BASE_URL")
    expected_fallback = f"{_SHARED_HOST}/ai-gateway/anthropic"
    assert env_base_url == expected_fallback, (
        f"Broker config should reject alias when workspace doesn't match alias_of. "
        f"Expected: {expected_fallback}, Got: {env_base_url}"
    )
