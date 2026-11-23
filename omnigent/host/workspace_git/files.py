"""A workspace's file list from Git, through the hardened runner only.

The workspace API's file search reads its candidates here instead of from the
REST routes' change registry, whose Git calls run outside the runner and may
write the repository's configuration. This never writes the index or config.
"""

from __future__ import annotations

import itertools
import operator
import os
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
# The tracked list's own cap: a repository with 1.6 million files lists 136 MB.
TRACKED_LISTING_BYTES = 512 * 1024 * 1024
# Tracked paths kept across workspaces; older workspaces' lists go first.
_MAX_CACHED_TRACKED_PATHS = 4_000_000
# Records that could name a path outside the workspace, framed by NULs.
_UNSAFE_RECORD_PARTS = (b"\0/", b"//", b"/./", b"/../", b"\0./", b"\0../", b"/.\0", b"/..\0")
_UNSAFE_RECORDS = (b"\0.\0", b"\0..\0", b"\0\0", b"/\0")


@dataclass(frozen=True)
class FileListing:
    """The files Git knows beneath a workspace, relative to it.

    :param tracked: Paths in the index, in path order. Shared with later
        listings while the paths are unchanged, so it must not be modified.
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
    """The paths in ``git ls-files -z`` output that stay inside the workspace, once each."""
    # Decoded at once when no record could leave the workspace; else record by record.
    framed = b"\0" + output
    if not any(part in framed for part in (*_UNSAFE_RECORD_PARTS, *_UNSAFE_RECORDS)):
        try:
            records = output.decode().split("\0")
        except UnicodeDecodeError:
            pass
        else:
            records.pop()  # empty, or cut off at the cap
            return list(dict.fromkeys(records))
    return _checked_paths(output)


def _checked_paths(output: bytes) -> list[str]:
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


class _TrackedListReader:
    """Parses ``git ls-files -z`` output as it arrives, reusing ``kept`` while it matches.

    Git lists in path order, so an unmerged file's repeated records are adjacent.
    """

    def __init__(self, kept: list[str] | None) -> None:
        self._kept = kept if kept is not None else []
        self._matched = 0  # leading paths of ``kept`` listed again so far
        # The paths listed, once they differ from ``kept``.
        self._built: list[str] | None = None if kept is not None else []
        self._rest = bytearray()  # a record not yet ended by its NUL

    def feed(self, chunk: bytes) -> None:
        # Only the new chunk is searched, and a long record is copied once as it grows.
        end = chunk.rfind(b"\0") + 1
        if not end:
            self._rest += chunk
            return
        records = bytes(self._rest) + chunk[:end] if self._rest else chunk[:end]
        self._rest = bytearray(chunk[end:])
        self._add(_paths(records))

    def _add(self, paths: list[str]) -> None:
        if self._built is not None:
            self._built.extend(paths)
            return
        kept, matched = self._kept, self._matched
        for index, path in enumerate(paths):
            if matched < len(kept) and kept[matched] == path:
                matched += 1
            elif not (matched and kept[matched - 1] == path):
                self._built = [*kept[:matched], *paths[index:]]
                return
        self._matched = matched

    def finish(self) -> list[str]:
        """The paths in path order, once each: ``kept`` itself when they're the same.

        A record cut off at the cap is left out.
        """
        if self._built is None:
            if self._matched == len(self._kept):
                return self._kept
            self._built = self._kept[: self._matched]
        paths = self._built
        if not all(map(operator.le, paths, itertools.islice(paths, 1, None))):
            paths.sort()  # Git lists in byte order, which is path order for UTF-8
        if any(map(operator.eq, paths, itertools.islice(paths, 1, None))):
            paths = list(dict.fromkeys(paths))
        return paths


class WorkspaceFiles:
    """Lists one workspace's files through the runner.

    :param root: The workspace directory, resolved.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        # The index state, its paths, and whether the listing hit the cap.
        self._tracked: tuple[tuple[int, ...], list[str], bool] | None = None
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
        tracked, tracked_incomplete = await self._list_tracked(repository, deadline)
        untracked, untracked_incomplete = await self._list_untracked(
            repository, deadline, retry=retry_untracked
        )
        return FileListing(
            tracked=tracked,
            untracked=untracked,
            incomplete=tracked_incomplete or untracked_incomplete,
        )

    async def _list_tracked(
        self, repository: GitRepository, deadline: Deadline
    ) -> tuple[list[str], bool]:
        # The index alone decides the listing, so an unchanged index reuses it.
        index = _index_state(repository.git_directory)
        cached = self._tracked
        if index is not None and cached is not None and cached[0] == index:
            return cached[1], cached[2]
        # Parsed as it arrives, so the output is never held whole. Git rewrites the
        # index often without changing its paths, e.g. for `git status`: then the
        # kept list is returned, and nothing new is held.
        reader = _TrackedListReader(cached[1] if cached is not None else None)
        output = await repository.run_unchecked(
            ["ls-files", "--cached", "-z", "--", "."],
            deadline=deadline,
            stdout_bytes=TRACKED_LISTING_BYTES,
            allow_partial=True,
            stdout_sink=reader.feed,
        )
        if not output.success and not output.overflowed:
            raise failure(output, "ls-files")
        paths = reader.finish()
        if index is not None:
            self._tracked = (index, paths, output.overflowed)
            _keep_tracked_budget(self)
        return paths, output.overflowed

    def tracked_paths_held(self) -> int:
        """How many tracked paths this lister keeps for reuse."""
        return len(self._tracked[1]) if self._tracked is not None else 0

    def forget_tracked(self) -> None:
        """Drop the kept tracked list; the next listing reads it again."""
        self._tracked = None

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


def _index_state(git_directory: Path) -> tuple[int, ...] | None:
    """What changes whenever Git writes the index, or ``None`` without one."""
    try:
        found = os.stat(git_directory / "index")
    except OSError:
        return None
    return (found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns)


_by_root: OrderedDict[Path, WorkspaceFiles] = OrderedDict()


def workspace_files(root: Path) -> WorkspaceFiles:
    """The shared lister for ``root``, so its listings are reused across calls."""
    files = _by_root.pop(root, None) or WorkspaceFiles(root)
    _by_root[root] = files
    while len(_by_root) > _MAX_CACHED_WORKSPACES:
        _by_root.popitem(last=False)
    return files


def _keep_tracked_budget(loaded: WorkspaceFiles) -> None:
    """Past the budget, drop the least recently used workspaces' tracked lists but ``loaded``'s."""
    others = [lister for lister in _by_root.values() if lister is not loaded]
    held = loaded.tracked_paths_held() + sum(lister.tracked_paths_held() for lister in others)
    for lister in others:
        if held <= _MAX_CACHED_TRACKED_PATHS:
            break
        held -= lister.tracked_paths_held()
        lister.forget_tracked()
