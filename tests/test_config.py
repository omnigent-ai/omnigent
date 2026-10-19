"""Tests for shared Omnigent config loading."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import omnigent.config as _config_module
from omnigent.config import (
    _merge_effective_config,
    github_account_preference,
    global_config_path,
    load_effective_config,
    load_global_config,
    load_local_config,
    save_global_config,
    set_github_account_preference,
)
from omnigent.harness_startup_config import (
    resolve_harness_args,
    resolve_harness_command,
    resolve_harness_config,
)


def test_effective_config_keeps_user_harness_launch_settings(
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
        "harness:\n  default: codex\n  claude-sdk:\n    command: /local/claude\n"
        "  codex:\n    command: /local/codex\n    args: [--project-option]\n"
        "  pi:\n    command: /local/pi\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.chdir(project)

    cfg = load_effective_config()
    harness = cfg["harness"]
    assert harness["default"] == "codex"
    # User command wins over a conflicting project command override.
    assert harness["claude-sdk"] == {"command": "/global/claude"}
    assert harness["codex"] == {"args": ["--config", "k=v"]}
    assert "pi" not in harness
    monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    monkeypatch.delenv("HARNESS_CODEX_PATH", raising=False)
    assert resolve_harness_command("codex", default="codex", cfg=cfg) == "codex"
    assert resolve_harness_args("codex", ["--verbose"], cfg=cfg) == [
        "--config",
        "k=v",
        "--verbose",
    ]


def test_merge_effective_config_scalar_local_preserves_user_overrides() -> None:
    g = {"harness": {"default": "claude-sdk", "codex": {"args": ["x"]}}}
    loc = {"harness": "codex"}
    assert _merge_effective_config(g, loc) == {
        "harness": {"default": "codex", "codex": {"args": ["x"]}}
    }


@pytest.mark.parametrize("bad_default", [["not", "a", "string"], "", 1, None])
def test_merge_effective_config_ignores_invalid_project_default(bad_default: object) -> None:
    # A malformed or empty project default (untrusted input) must not propagate;
    # the user's harness settings stand so resolution still succeeds.
    g = {"harness": {"default": "claude-sdk", "codex": {"args": ["x"]}}}
    loc = {"harness": {"default": bad_default, "codex": {"command": "/evil"}}}
    assert _merge_effective_config(g, loc) == {
        "harness": {"default": "claude-sdk", "codex": {"args": ["x"]}}
    }


def test_merge_effective_config_scalar_global_project_default_wins() -> None:
    # A scalar user harness carries no launch settings to preserve, so the
    # project's default selection applies on top of an empty trusted mapping.
    g = {"harness": "claude-sdk"}
    loc = {"harness": {"default": "codex"}}
    assert _merge_effective_config(g, loc) == {"harness": {"default": "codex"}}


def test_merge_effective_config_no_harness_key_unchanged() -> None:
    assert _merge_effective_config({"model": "x"}, {"server": "y"}) == {
        "model": "x",
        "server": "y",
    }


def test_merge_effective_config_warns_once_on_dropped_project_overrides(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Dropped project launch overrides are invisible to the resolver, so the
    # merge logs them once per process to explain why they stopped applying.
    monkeypatch.setattr(_config_module, "_harness_override_warned", False)
    g = {"harness": {"default": "claude-sdk"}}
    loc = {
        "harness": {
            "default": "codex",
            "codex": {"command": "/evil"},
            "claude": {"args": ["x"]},
        }
    }
    with caplog.at_level(logging.WARNING, logger="omnigent.config"):
        _merge_effective_config(g, loc)
        _merge_effective_config(g, loc)
    warnings = [r.getMessage() for r in caplog.records if "launch override" in r.getMessage()]
    assert warnings == [
        "ignoring project harness launch override(s): claude, codex (command/args are user-owned)"
    ]


def test_merge_effective_config_no_warning_without_project_overrides(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A project selecting only the default harness drops nothing; stay quiet.
    monkeypatch.setattr(_config_module, "_harness_override_warned", False)
    with caplog.at_level(logging.WARNING, logger="omnigent.config"):
        _merge_effective_config({"harness": {"default": "claude-sdk"}}, {"harness": "codex"})
    assert not [r for r in caplog.records if "launch override" in r.getMessage()]


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
        "harness:\n  default: codex\n  claude-native:\n    command: project-claude\n"
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
        "harness": {
            # The workspace's allowed ``default`` is read, proving the explicit
            # workspace was loaded; its claude-native launch override is dropped
            # so the user's command/args stand.
            "default": "codex",
            "claude-native": {"command": "global-claude", "args": ["--verbose"]},
        },
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


def test_load_local_config_returns_empty_when_cwd_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """load_local_config returns {} when the process cwd no longer exists."""
    deleted = tmp_path / "deleted_dir"
    deleted.mkdir()
    monkeypatch.chdir(deleted)
    deleted.rmdir()
    # Reset the module-level warn-once guard so this test is self-contained.
    monkeypatch.setattr(_config_module, "_cwd_missing_warned", False)

    result = load_local_config()

    assert result == {}


def test_load_effective_config_returns_global_when_cwd_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """load_effective_config returns global config when the cwd has been removed."""
    config_home = tmp_path / "home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text("model: global-model\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    deleted = tmp_path / "deleted_dir"
    deleted.mkdir()
    monkeypatch.chdir(deleted)
    deleted.rmdir()
    monkeypatch.setattr(_config_module, "_cwd_missing_warned", False)

    cfg = load_effective_config()

    assert cfg == {"model": "global-model"}


def test_load_local_config_explicit_path_unaffected_by_missing_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Passing an explicit path to load_local_config bypasses cwd lookup."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("profile: explicit\n")

    deleted = tmp_path / "deleted_dir"
    deleted.mkdir()
    monkeypatch.chdir(deleted)
    deleted.rmdir()
    monkeypatch.setattr(_config_module, "_cwd_missing_warned", False)

    result = load_local_config(path=cfg_file)

    assert result == {"profile": "explicit"}


# The native terminal resolves its launch binary/args like the helpers below
# (omnigent/runner/native/orchestration.py). A project .omnigent/config.yaml may
# pick a default harness but must not supply the executable/args — those are user-only.

_USER_CLAUDE = "/user/trusted/claude-wrapper"
_USER_ARGS = ["--user-arg"]


def _resolve_native_launch(workspace: Path) -> tuple[str, list[str], str | None]:
    cfg = load_effective_config(workspace=workspace)
    command = resolve_harness_command("claude-native", default="claude", cfg=cfg)
    args = resolve_harness_args("claude-native", (), cfg=cfg)
    default_harness, _ = resolve_harness_config(cfg)
    return command, args, default_harness


def _write_user_native_config(config_home: Path) -> None:
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        f"harness:\n  claude-native:\n    command: {_USER_CLAUDE}\n    args: [--user-arg]\n"
    )


def test_project_config_cannot_replace_user_native_command_or_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_home = tmp_path / "home"
    workspace = tmp_path / "project"
    _write_user_native_config(config_home)
    (workspace / ".omnigent").mkdir(parents=True)
    (workspace / ".omnigent" / "config.yaml").write_text(
        "harness:\n"
        "  claude-native:\n"
        "    command: /project/project-claude\n"
        "    args: [--from-project-config]\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("OMNIGENT_CLAUDE_PATH", raising=False)

    command, args, _ = _resolve_native_launch(workspace)

    assert command == _USER_CLAUDE
    assert args == _USER_ARGS


def test_project_scalar_default_preserves_user_native_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_home = tmp_path / "home"
    workspace = tmp_path / "project"
    _write_user_native_config(config_home)
    (workspace / ".omnigent").mkdir(parents=True)
    # Legacy scalar form: the project selects a default harness only.
    (workspace / ".omnigent" / "config.yaml").write_text("harness: codex-native\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("OMNIGENT_CLAUDE_PATH", raising=False)

    command, args, default_harness = _resolve_native_launch(workspace)

    assert default_harness == "codex-native"
    assert command == _USER_CLAUDE
    assert args == _USER_ARGS


def test_user_native_overrides_apply_without_project_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_home = tmp_path / "home"
    workspace = tmp_path / "project"
    _write_user_native_config(config_home)
    workspace.mkdir()
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("OMNIGENT_CLAUDE_PATH", raising=False)

    command, args, _ = _resolve_native_launch(workspace)

    assert command == _USER_CLAUDE
    assert args == _USER_ARGS
