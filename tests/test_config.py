"""Tests for shared Omnigent config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.config import (
    _merge_effective_config,
    env_passthrough_names,
    global_config_path,
    load_effective_config,
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


# ---------------------------------------------------------------------------
# env_passthrough_names — host/project-level allowlist that unions with the
# per-spec ``os_env.sandbox.env_passthrough`` list at spawn time (#5442).
# ---------------------------------------------------------------------------


def test_env_passthrough_names_empty_when_no_config() -> None:
    """No config on either side → empty tuple, never a None or a KeyError."""
    assert env_passthrough_names(global_cfg={}, local_cfg={}) == ()


def test_env_passthrough_names_reads_global_only() -> None:
    g = {"env_passthrough": ["FOO", "BAR"]}
    assert env_passthrough_names(global_cfg=g, local_cfg={}) == ("BAR", "FOO")


def test_env_passthrough_names_reads_project_only() -> None:
    loc = {"env_passthrough": ["PROJ_VAR"]}
    assert env_passthrough_names(global_cfg={}, local_cfg=loc) == ("PROJ_VAR",)


def test_env_passthrough_names_unions_global_and_project() -> None:
    """Project names are honoured on top of global ones — neither removes the other."""
    g = {"env_passthrough": ["FOO", "BAR"]}
    loc = {"env_passthrough": ["BAZ", "FOO"]}
    # Union, sorted, deduped
    assert env_passthrough_names(global_cfg=g, local_cfg=loc) == ("BAR", "BAZ", "FOO")


def test_env_passthrough_names_ignores_non_list_values() -> None:
    """A misconfigured scalar / mapping under env_passthrough shouldn't crash."""
    g = {"env_passthrough": "OOPS_SCALAR"}
    loc = {"env_passthrough": {"OOPS": "MAP"}}
    assert env_passthrough_names(global_cfg=g, local_cfg=loc) == ()


def test_env_passthrough_names_filters_empty_names() -> None:
    """Empty strings in the list are dropped, not admitted as a wildcard."""
    g = {"env_passthrough": ["FOO", "", None, "BAR"]}
    assert env_passthrough_names(global_cfg=g, local_cfg={}) == ("BAR", "FOO")


def test_env_passthrough_names_loads_from_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default path: reads from OMNIGENT_CONFIG_HOME + cwd/.omnigent."""
    config_home = tmp_path / "home"
    project = tmp_path / "project"
    config_home.mkdir()
    (project / ".omnigent").mkdir(parents=True)
    (config_home / "config.yaml").write_text("env_passthrough: [GLOBAL_A]\n")
    (project / ".omnigent" / "config.yaml").write_text("env_passthrough: [PROJ_B]\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.chdir(project)

    assert env_passthrough_names() == ("GLOBAL_A", "PROJ_B")
