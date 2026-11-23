"""A workspace's file list from Git, through the hardened runner only.

The workspace API's file search reads its candidates here instead of from the
REST routes' change registry, whose Git calls run outside the runner and may
write the repository's configuration. This never writes the index or config.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from omnigent.host.workspace_git.runner import (
    LISTING_OUTPUT_BYTES,
    Deadline,
    GitError,
    GitRepository,
    failure,
)

# How long an untracked listing is reused; `retry_untracked` skips it.
_UNTRACKED_TTL_S = 5.0
_MAX_CACHED_WORKSPACES = 64


@dataclass(frozen=True)
class FileListing:
    """The files Git knows beneath a workspace, relative to it.

    :param tracked: Paths in the index.
    :param untracked: Untracked, unignored files; a nested repository is
        its directory. ``None`` when they couldn't be listed.
    :param incomplete: A listing stopped at its size cap.
    """

    tracked: list[str]
    untracked: list[str] | None
    incomplete: bool


def is_inside(path: str) -> bool:
    """Whether a path Git listed stays beneath the directory it was listed from.

    A crafted index could name ``../x`` or an absolute path.
    """
    return (
        bool(path)
        and not path.startswith("/")
        and all(part not in ("", ".", "..") for part in path.split("/"))
    )


def _paths(output: bytes) -> list[str]:
    records = output.split(b"\0")
    # The last record is empty after a complete listing, or cut off after a partial one.
    records.pop()
    paths: dict[str, None] = {}
    for record in records:
        try:
            path = record.decode()
        except UnicodeDecodeError:
            continue  # no response can carry a name that isn't UTF-8
        path = path.rstrip("/")  # a nested repository ends in "/"
        if not is_inside(path):
            continue
        # Unmerged files repeat once per stage.
        paths[path] = None
    return list(paths)


class WorkspaceFiles:
    """Lists one workspace's files through the runner.

    :param root: The workspace directory, resolved.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._untracked: tuple[float, list[str], bool] | None = None

    async def list_files(
        self, deadline: Deadline, *, retry_untracked: bool = False
    ) -> FileListing | None:
        """List the workspace's files, or ``None`` outside a Git repository.

        :param deadline: The RPC's deadline, shared by every command.
        :param retry_untracked: List untracked files again even if a recent list exists.
        :raises GitError: When the repository or its tracked files can't be read.
        """
        repository = await GitRepository.discover(self._root, deadline)
        if repository is None:
            return None
        tracked = await repository.run_unchecked(
            ["ls-files", "--cached", "-z", "--", "."],
            deadline=deadline,
            stdout_bytes=LISTING_OUTPUT_BYTES,
            allow_partial=True,
        )
        if not tracked.success and not tracked.overflowed:
            raise failure(tracked, "ls-files")
        untracked, untracked_incomplete = await self._list_untracked(
            repository, deadline, retry=retry_untracked
        )
        return FileListing(
            tracked=_paths(tracked.stdout),
            untracked=untracked,
            incomplete=tracked.overflowed or untracked_incomplete,
        )

    async def _list_untracked(
        self, repository: GitRepository, deadline: Deadline, *, retry: bool
    ) -> tuple[list[str] | None, bool]:
        cached = self._untracked
        if cached is not None and not retry and time.monotonic() - cached[0] < _UNTRACKED_TTL_S:
            return cached[1], cached[2]
        try:
            output = await repository.run_unchecked(
                ["ls-files", "--others", "--exclude-standard", "-z", "--", "."],
                deadline=deadline,
                stdout_bytes=LISTING_OUTPUT_BYTES,
                allow_partial=True,
            )
        except GitError:
            return None, False
        if not output.success and not output.overflowed:
            return None, False
        paths = _paths(output.stdout)
        self._untracked = (time.monotonic(), paths, output.overflowed)
        return paths, output.overflowed


_by_root: OrderedDict[Path, WorkspaceFiles] = OrderedDict()


def workspace_files(root: Path) -> WorkspaceFiles:
    """The shared lister for ``root``, so its untracked list is reused across calls."""
    files = _by_root.pop(root, None) or WorkspaceFiles(root)
    _by_root[root] = files
    while len(_by_root) > _MAX_CACHED_WORKSPACES:
        _by_root.popitem(last=False)
    return files
