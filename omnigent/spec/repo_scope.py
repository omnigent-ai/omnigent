"""Walk a workspace up to its repository root, as vendor CLIs do for project config."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal


def repo_scope_dirs(
    workspace: Path,
    *,
    home: Path | None = None,
    outside_repo: Literal["self", "ancestors"],
) -> list[Path]:
    """
    Return *workspace* and its ancestors up to the repository root, nearest first.

    The walk ends at the first directory holding ``.git`` (a worktree's ``.git``
    file counts), and stops short of *home* when one is given. Outside a
    repository, ``"self"`` keeps only the workspace and ``"ancestors"`` keeps
    every directory walked.

    :param workspace: The CLI's working directory, e.g. ``/repo/app``.
    :param home: The user home directory, which is never returned and ends the
        walk, or ``None`` to walk past it.
    :param outside_repo: What to return when no ``.git`` is found.
    :returns: Real paths of the directories in scope, nearest first.
    """
    # realpath, unlike Path.resolve, tolerates symlink loops.
    current = Path(os.path.realpath(workspace))
    stop = Path(os.path.realpath(home)) if home is not None else None
    walked: list[Path] = []
    for directory in (current, *current.parents):
        if directory == stop:
            break
        walked.append(directory)
        if (directory / ".git").exists():
            return walked
    return walked if outside_repo == "ancestors" else walked[:1]
