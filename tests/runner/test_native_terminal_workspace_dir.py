"""Workspace resolution for native-terminal launches must not read a dead cwd."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.runner.native.orchestration import _runner_workspace_dir


def test_env_wins_without_touching_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A set workspace must be returned even when the process cwd is unreadable.

    ``os.environ.get(name, str(Path.cwd()))`` evaluated the default eagerly, so a
    runner whose worktree had been removed under it died with ``FileNotFoundError``
    from ``os.getcwd()`` while the answer sat in the environment all along.
    """
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))

    def _dead_cwd() -> Path:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(Path, "cwd", staticmethod(_dead_cwd))

    assert _runner_workspace_dir() == str(tmp_path)


def test_falls_back_to_cwd_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)

    assert Path(_runner_workspace_dir()).resolve() == tmp_path.resolve()


def test_empty_env_value_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", "")
    monkeypatch.chdir(tmp_path)

    assert Path(_runner_workspace_dir()).resolve() == tmp_path.resolve()


def test_dead_cwd_without_env_reports_the_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)

    def _dead_cwd() -> Path:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(Path, "cwd", staticmethod(_dead_cwd))

    with pytest.raises(RuntimeError, match="no longer exists"):
        _runner_workspace_dir()


def test_removed_worktree_is_the_real_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live failure: cwd is a directory that was deleted under the process."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.chdir(worktree)
    worktree.rmdir()
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))

    with pytest.raises(FileNotFoundError):
        os.getcwd()
    assert _runner_workspace_dir() == str(tmp_path)
