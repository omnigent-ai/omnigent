"""Tests for resolving Claude Code's files the way the Claude CLI does."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.claude_paths import claude_config_dir, claude_json_path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at a temp dir.

    :param tmp_path: Pytest temp dir.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: The fake home, e.g. ``tmp_path / "home"``.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_claude_config_dir_defaults_to_dot_claude(home: Path) -> None:
    """Without ``CLAUDE_CONFIG_DIR`` Claude keeps its state in ``~/.claude``."""
    assert claude_config_dir() == home / ".claude"


def test_claude_config_dir_follows_claude_config_dir(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile selected via ``CLAUDE_CONFIG_DIR`` replaces ``~/.claude`` entirely."""
    profile = tmp_path / "work-profile"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))

    assert claude_config_dir() == profile


def test_claude_config_dir_expands_tilde(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``~``-relative ``CLAUDE_CONFIG_DIR`` resolves against the user's home."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/profiles/work")

    assert claude_config_dir() == home / "profiles" / "work"


def test_claude_config_dir_treats_empty_value_as_unset(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty ``CLAUDE_CONFIG_DIR`` falls back to ``~/.claude``, as the CLI does."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")

    assert claude_config_dir() == home / ".claude"


def test_claude_json_defaults_to_home(home: Path) -> None:
    """The user-scope config sits beside ``~/.claude``, not inside it."""
    assert claude_json_path() == home / ".claude.json"


def test_claude_json_moves_into_claude_config_dir(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``CLAUDE_CONFIG_DIR`` set, the CLI reads ``.claude.json`` from inside it."""
    profile = tmp_path / "work-profile"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))

    assert claude_json_path() == profile / ".claude.json"
