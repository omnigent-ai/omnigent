"""Tests for shared Omnigent config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.config import global_config_path, load_effective_config


def test_global_config_path_respects_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    assert global_config_path() == tmp_path / "config.yaml"


def test_effective_config_merges_project_over_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_home = tmp_path / "home"
    project = tmp_path / "project"
    config_home.mkdir()
    (project / ".omnigent").mkdir(parents=True)
    (config_home / "config.yaml").write_text("profile: global\nmodel: global-model\n")
    (project / ".omnigent" / "config.yaml").write_text("profile: local\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.chdir(project)

    assert load_effective_config() == {"profile": "local", "model": "global-model"}
