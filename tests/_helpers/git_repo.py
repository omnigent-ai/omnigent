"""Real git repositories for tests that exercise worktree behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path

_IDENTITY = ("-c", "user.email=test@example.com", "-c", "user.name=test")


def repo_with_worktree(root: Path, *, branch: str = "feature") -> tuple[Path, Path]:
    """Create a repo with one commit and one linked worktree.

    :param root: Fresh directory to create them in.
    :param branch: Branch checked out in the worktree.
    :returns: ``(main checkout, linked worktree)``.
    """
    repo, worktree = root / "repo", root / "repo-worktrees" / branch
    repo.mkdir(parents=True)
    for args in (
        ["init", "-q", "-b", "main"],
        [*_IDENTITY, "commit", "-q", "--allow-empty", "-m", "init"],
        ["worktree", "add", "-q", "-b", branch, str(worktree)],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo, worktree
