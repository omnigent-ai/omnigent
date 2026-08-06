"""Tests for :func:`omnigent.onboarding.acp_auth.workspace_acp_agents`.

The reader sources the *project-level* ``.omnigent/config.yaml`` only — it must
never merge the global config (every returned entry is attributed to the repo)
and must never raise (a broken repo config can't break discovery).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.onboarding.acp_auth import workspace_acp_agents


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """Point OMNIGENT_CONFIG_HOME away from the real global config."""
    config_home = tmp_path_factory.mktemp("config-home")
    (config_home / "config.yaml").write_text(
        yaml.safe_dump({"acp": {"agents": [{"name": "Global Agent", "command": "global --acp"}]}})
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))


def _write_workspace_config(workspace: Path, payload: object) -> None:
    (workspace / ".omnigent").mkdir(parents=True, exist_ok=True)
    (workspace / ".omnigent" / "config.yaml").write_text(
        payload if isinstance(payload, str) else yaml.safe_dump(payload)
    )


def test_reads_repo_declared_agents(tmp_path: Path) -> None:
    _write_workspace_config(
        tmp_path,
        {
            "acp": {
                "agents": [
                    {"name": "Repo Echo", "command": "echo-agent --acp"},
                    {"name": "Repo Goose", "command": "goose acp", "model": "gpt-5.3"},
                ]
            }
        },
    )
    entries = workspace_acp_agents(tmp_path)
    assert [(e.slug, e.name, e.command) for e in entries] == [
        ("repo-echo", "Repo Echo", "echo-agent --acp"),
        ("repo-goose", "Repo Goose", "goose acp"),
    ]
    assert entries[1].model == "gpt-5.3"


def test_global_config_is_not_merged(tmp_path: Path) -> None:
    """Repo discovery must not surface globally-configured agents."""
    _write_workspace_config(tmp_path, {"acp": {"agents": []}})
    assert workspace_acp_agents(tmp_path) == []


def test_missing_config_returns_empty(tmp_path: Path) -> None:
    assert workspace_acp_agents(tmp_path) == []
    assert workspace_acp_agents(tmp_path / "does-not-exist") == []


def test_malformed_yaml_returns_empty(tmp_path: Path) -> None:
    _write_workspace_config(tmp_path, "acp: [unclosed")
    assert workspace_acp_agents(tmp_path) == []


def test_non_mapping_block_returns_empty(tmp_path: Path) -> None:
    _write_workspace_config(tmp_path, {"acp": "not-a-mapping"})
    assert workspace_acp_agents(tmp_path) == []


def test_slug_collisions_get_suffixes(tmp_path: Path) -> None:
    _write_workspace_config(
        tmp_path,
        {
            "acp": {
                "agents": [
                    {"name": "Helper", "command": "one --acp"},
                    {"name": "helper!", "command": "two --acp"},
                ]
            }
        },
    )
    assert [e.slug for e in workspace_acp_agents(tmp_path)] == ["helper", "helper-2"]
