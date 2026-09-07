"""Tests for shared Omnigent config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.config import _merge_effective_config, global_config_path, load_effective_config


def test_effective_config_deep_merges_harness_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_home = tmp_path / "home"
    project = tmp_path / "project"
    config_home.mkdir()
    (project / ".omnigent").mkdir(parents=True)
    (config_home / "config.yaml").write_text(
        "harness:\n  default: claude-sdk\n  claude-sdk:\n    command: /global/claude\n"
        "  codex:\n    args: [--config, k=v]\n"
    )
    (project / ".omnigent" / "config.yaml").write_text(
        "harness:\n  codex:\n    command: /local/codex\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.chdir(project)

    cfg = load_effective_config()
    harness = cfg["harness"]
    assert harness["default"] == "claude-sdk"
    # Global-only entry preserved (a flat merge would have dropped it).
    assert harness["claude-sdk"] == {"command": "/global/claude"}
    # Local per-harness entry augments the global one: local command wins,
    # global args preserved (per-field, not whole-entry replace).
    assert harness["codex"] == {"args": ["--config", "k=v"], "command": "/local/codex"}


def test_merge_effective_config_scalar_local_overrides_mapping_global() -> None:
    # A scalar on either side is an explicit whole-value override: the
    # shallow {**global, **local} result holds (no deep-merge).
    g = {"harness": {"default": "claude-sdk", "codex": {"args": ["x"]}}}
    loc = {"harness": "codex"}
    assert _merge_effective_config(g, loc) == {"harness": "codex"}


def test_merge_effective_config_scalar_global_no_deep_merge() -> None:
    # Global scalar + local mapping: local (mapping) wins outright as a
    # whole-value replace — only deep-merge when BOTH are mappings.
    g = {"harness": "claude-sdk"}
    loc = {"harness": {"default": "codex"}}
    assert _merge_effective_config(g, loc) == {"harness": {"default": "codex"}}


def test_merge_effective_config_no_harness_key_unchanged() -> None:
    assert _merge_effective_config({"model": "x"}, {"server": "y"}) == {
        "model": "x",
        "server": "y",
    }


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
