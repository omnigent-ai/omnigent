from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from omnigent.spec.repo_scope import repo_scope_dirs


@pytest.mark.parametrize("outside_repo", ["self", "ancestors"])
def test_walk_ends_at_the_repository_root(
    tmp_path: Path, outside_repo: Literal["self", "ancestors"]
) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "pkg" / "app"
    workspace.mkdir(parents=True)
    # A worktree's .git file marks the root like a .git directory.
    (repo / ".git").write_text("gitdir: elsewhere\n")
    link = tmp_path / "link"
    link.symlink_to(workspace)

    assert repo_scope_dirs(link, outside_repo=outside_repo) == [
        workspace.resolve(),
        (repo / "pkg").resolve(),
        repo.resolve(),
    ]


def test_outside_repo_selects_the_workspace_or_its_ancestors(tmp_path: Path) -> None:
    workspace = tmp_path / "parent" / "app"
    workspace.mkdir(parents=True)

    # Stopping at tmp_path keeps any checkout around it out of the walk.
    assert repo_scope_dirs(workspace, home=tmp_path, outside_repo="self") == [workspace.resolve()]
    assert repo_scope_dirs(workspace, home=tmp_path, outside_repo="ancestors") == [
        workspace.resolve(),
        (tmp_path / "parent").resolve(),
    ]


@pytest.mark.parametrize("outside_repo", ["self", "ancestors"])
def test_home_ends_the_walk_before_an_enclosing_repository(
    tmp_path: Path, outside_repo: Literal["self", "ancestors"]
) -> None:
    (tmp_path / ".git").mkdir()
    home = tmp_path / "home"
    workspace = home / "project"
    workspace.mkdir(parents=True)

    assert repo_scope_dirs(workspace, home=home, outside_repo=outside_repo) == [
        workspace.resolve()
    ]
    assert repo_scope_dirs(home, home=home, outside_repo=outside_repo) == []
