"""Persistent TUI user config tests."""

from __future__ import annotations

from unittest.mock import patch

from omnigent_ui_sdk.terminal import (
    DEFAULT_USER_CONFIG,
    UserConfig,
    UserConfigError,
    load_user_config,
    save_user_config,
    state_dir,
    update_user_config,
    user_config_path,
)


def test_user_config_path_uses_shared_state_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    assert state_dir() == tmp_path / ".omnigent"
    assert user_config_path() == tmp_path / ".omnigent" / "config.yaml"


def test_user_config_path_accepts_explicit_state_dir(tmp_path) -> None:
    assert user_config_path(tmp_path) == tmp_path / "config.yaml"


def test_load_missing_config_returns_default(tmp_path) -> None:
    assert load_user_config(tmp_path / "config.yaml") == DEFAULT_USER_CONFIG


def test_save_and_load_user_config_round_trips_yaml(tmp_path) -> None:
    path = tmp_path / "state" / "config.yaml"

    written = save_user_config(UserConfig(theme="dark"), path)

    assert written == path
    assert path.read_text(encoding="utf-8") == (
        "# Omnigent user configuration\ntui:\n  theme: dark\n"
    )
    assert load_user_config(path) == UserConfig(theme="dark")


def test_save_preserves_sibling_cli_keys(tmp_path) -> None:
    """TUI writes must not clobber top-level CLI keys (default_agent, profile, …)."""
    path = tmp_path / "config.yaml"
    path.write_text("default_agent: foo\nprofile: bar\n", encoding="utf-8")

    save_user_config(UserConfig(theme="dark"), path)

    assert path.read_text(encoding="utf-8") == (
        "# Omnigent user configuration\ndefault_agent: foo\nprofile: bar\ntui:\n  theme: dark\n"
    )


def test_save_default_removes_tui_but_keeps_siblings(tmp_path) -> None:
    """Resetting to DEFAULT drops ``tui:`` while preserving sibling keys."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "default_agent: foo\ntui:\n  theme: dark\n",
        encoding="utf-8",
    )

    save_user_config(DEFAULT_USER_CONFIG, path)

    assert path.read_text(encoding="utf-8") == (
        "# Omnigent user configuration\ndefault_agent: foo\n"
    )


def test_load_invalid_theme_fails_loud(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("tui:\n  theme: sepia\n", encoding="utf-8")

    try:
        load_user_config(path)
    except UserConfigError as exc:
        assert "must be one of dark, light" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("load_user_config accepted an invalid theme")


def test_update_user_config_normalizes_and_persists(tmp_path) -> None:
    path = tmp_path / "config.yaml"

    config = update_user_config(path, theme=" LIGHT ")

    assert config == UserConfig(theme="light")
    assert load_user_config(path) == UserConfig(theme="light")


def test_save_default_user_config_round_trips_without_tui_table(tmp_path) -> None:
    path = tmp_path / "config.yaml"

    save_user_config(DEFAULT_USER_CONFIG, path)

    assert path.read_text(encoding="utf-8") == "# Omnigent user configuration\n"
    assert load_user_config(path) == DEFAULT_USER_CONFIG


def test_save_user_config_cleans_up_temp_file_when_replace_fails(tmp_path) -> None:
    path = tmp_path / "config.yaml"

    with patch("pathlib.Path.replace", side_effect=OSError("rename failed")):
        try:
            save_user_config(UserConfig(theme="dark"), path)
        except UserConfigError as exc:
            assert "Failed to write TUI user config" in str(exc)
        else:  # pragma: no cover - defensive assertion
            raise AssertionError("save_user_config swallowed replace failure")

    assert not list(tmp_path.glob("config.yaml.tmp.*"))
