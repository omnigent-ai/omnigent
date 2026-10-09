"""Tests for shared Omnigent config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.config import (
    _merge_effective_config,
    github_account_preference,
    global_config_path,
    load_effective_config,
    load_global_config,
    save_global_config,
    set_github_account_preference,
)


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


@pytest.mark.parametrize("cwd_available", [False, True])
def test_effective_config_uses_explicit_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cwd_available: bool
) -> None:
    config_home = tmp_path / "home"
    workspace = tmp_path / "project"
    other_project = tmp_path / "other"
    config_home.mkdir()
    (workspace / ".omnigent").mkdir(parents=True)
    (other_project / ".omnigent").mkdir(parents=True)
    (config_home / "config.yaml").write_text(
        "profile: global\nharness:\n  claude-native:\n    command: global-claude\n"
        "    args: [--verbose]\n"
    )
    (workspace / ".omnigent" / "config.yaml").write_text(
        "harness:\n  claude-native:\n    command: project-claude\n"
    )
    (other_project / ".omnigent" / "config.yaml").write_text("profile: wrong-project\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.chdir(other_project)

    def missing_cwd() -> Path:
        raise FileNotFoundError("process cwd was removed")

    if not cwd_available:
        monkeypatch.setattr(Path, "cwd", missing_cwd)

    assert load_effective_config(workspace=workspace) == {
        "profile": "global",
        "harness": {"claude-native": {"command": "project-claude", "args": ["--verbose"]}},
    }


def test_effective_config_workspace_without_project_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_home = tmp_path / "home"
    workspace = tmp_path / "project"
    config_home.mkdir()
    workspace.mkdir()
    (config_home / "config.yaml").write_text("profile: global\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    assert load_effective_config(workspace=workspace) == {"profile": "global"}


def test_github_account_preference_round_trip(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    key = "/Users/daniel.lok/omnigent"
    assert github_account_preference(key, cfg) is None
    set_github_account_preference(key, "daniellok-db", cfg)
    assert github_account_preference(key, cfg) == "daniellok-db"
    # The key is a filesystem path, matched verbatim — NOT case-folded.
    assert github_account_preference(key.upper(), cfg) is None


def test_set_github_account_preference_preserves_other_workspaces(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    set_github_account_preference("/Users/daniel.lok/omnigent", "daniellok-db", cfg)
    set_github_account_preference("/Users/daniel.lok/mlflow", "daniel-lok_data", cfg)
    # A second workspace's pref doesn't clobber the first.
    assert github_account_preference("/Users/daniel.lok/omnigent", cfg) == "daniellok-db"
    assert github_account_preference("/Users/daniel.lok/mlflow", cfg) == "daniel-lok_data"


def test_set_github_account_preference_clear(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    key = "/Users/daniel.lok/omnigent"
    set_github_account_preference(key, "daniellok-db", cfg)
    set_github_account_preference(key, "", cfg)  # empty clears the entry
    assert github_account_preference(key, cfg) is None


def test_save_global_config_preserves_unrelated_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    set_github_account_preference("/Users/daniel.lok/omnigent", "daniellok-db", cfg)
    save_global_config({"default_agent": "/x/agent.yaml"}, path=cfg)
    # The general writer merges rather than truncating: both keys survive.
    assert github_account_preference("/Users/daniel.lok/omnigent", cfg) == "daniellok-db"
    assert load_global_config(cfg).get("default_agent") == "/x/agent.yaml"


def test_save_global_config_respects_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    set_github_account_preference("/Users/daniel.lok/omnigent", "daniellok-db")
    # Written to (and read back from) OMNIGENT_CONFIG_HOME/config.yaml.
    assert (tmp_path / "config.yaml").exists()
    assert github_account_preference("/Users/daniel.lok/omnigent") == "daniellok-db"
