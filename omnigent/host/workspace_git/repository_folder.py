"""The folder a Git request compares: the workspace, or a repository inside it.

A request may name a directory relative to the workspace root, such as a worktree
checked out inside it. It is confined the way file reads are, and its repository must be
the workspace's own, or a linked worktree of it whose working tree is inside the
workspace and is the one Git registered for it. Such a worktree shares the workspace
repository's objects and refs, so naming it reads nothing the workspace root doesn't. A
nested, independent repository is refused, so a crafted link, ``.git`` file, ``commondir``,
``core.worktree``, symlinked store or alternates file can't point a comparison at another
place on the host. A workspace inside a larger repository compares only its root.
"""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from connectrpc.code import Code
from connectrpc.errors import ConnectError

from omnigent.host.workspace_git.runner import (
    OUTSIDE_ANY_REPOSITORY,
    CheckedRepository,
    Deadline,
    GitError,
    GitRepository,
    UnreadableRepository,
)
from omnigent.host.workspace_git.working_copy import LISTING_DEADLINE_S
from omnigent.workspace_fs import WorkspaceReader, WorkspaceReaderError


@dataclass(frozen=True)
class RepositoryFolder:
    """A checked folder, and the repository found there when one was checked.

    :param path: The workspace root, or the resolved directory inside it.
    :param repository: The repository the checks passed for, which the Git calls reuse
        rather than looking it up again; ``OUTSIDE_ANY_REPOSITORY`` for a folder outside
        any, which they answer as not a repository; an ``UnreadableRepository`` when Git
        couldn't read it, which they report as they would for the workspace; ``None`` for
        the workspace root, which they discover as before.
    """

    path: Path
    repository: CheckedRepository = None


def _refuse(repository: str, reason: str) -> ConnectError:
    # Names only what the caller sent, never a resolved path.
    return ConnectError(Code.INVALID_ARGUMENT, f"Repository {repository!r} {reason}")


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


# Far beyond any path Git writes; a larger record isn't one.
_RECORD_LIMIT_BYTES = 64 * 1024


def _read_record(path: Path) -> str | None:
    """The text of the record at ``path``: None unless a regular file within the limit.

    Opened without blocking, so a named pipe planted there is refused rather than waited on.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as record:
            if not stat.S_ISREG(os.fstat(record.fileno()).st_mode):
                return None
            data = record.read(_RECORD_LIMIT_BYTES + 1)
    except OSError:
        return None
    if len(data) > _RECORD_LIMIT_BYTES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _names_top_level(git_directory: Path, top_level: Path) -> bool:
    recorded = (_read_record(git_directory / "gitdir") or "").strip()
    if not recorded:
        return False
    try:
        return (git_directory / recorded).resolve() == top_level / ".git"
    except (OSError, ValueError):
        # A NUL or another path the platform can't hold isn't a record of this folder.
        return False


async def _registered_at(git_directory: Path, top_level: Path) -> bool:
    """Whether Git's own record of a linked worktree, its ``gitdir`` file, names ``top_level``.

    A `.git` file can point at any registered worktree's metadata; only the worktree the
    record names may use it, so one elsewhere can't be compared through another folder.
    Read on a worker thread, as the Git calls run, so a slow disk doesn't hold the loop.
    """
    return await asyncio.to_thread(_names_top_level, git_directory, top_level)


def _places(repository: GitRepository) -> tuple[Path, Path, Path]:
    """Its Git directory, common directory and working tree, resolved."""
    return (
        repository.git_directory.resolve(),
        repository.common_directory.resolve(),
        repository.top_level.resolve(),
    )


async def repository_folder(reader: WorkspaceReader, repository: str) -> RepositoryFolder:
    """The folder a Git request named by ``repository`` compares, once checked.

    :param reader: The reader for the workspace the call may read.
    :param repository: A directory relative to the workspace root; empty for the root.
    :returns: The workspace root, or the resolved directory inside it with its repository.
    :raises ConnectError: INVALID_ARGUMENT when it escapes the workspace, even through a
        symlink, isn't a folder, lies in a Git directory, belongs to a repository that is
        neither the workspace's own nor a linked worktree of it registered at that folder,
        is such a worktree with its working tree outside the workspace, or the workspace
        is a folder inside a larger repository.
    """
    workspace = reader.root
    if not repository:
        return RepositoryFolder(workspace)
    try:
        folder = reader.resolve_directory(repository)
    except WorkspaceReaderError as error:
        raise _refuse(repository, "isn't a folder in the workspace") from error
    if not folder.is_dir():
        raise _refuse(repository, "isn't a folder in the workspace")
    if any(part.lower() == ".git" for part in folder.relative_to(workspace).parts):
        raise _refuse(repository, "is inside a Git directory")
    deadline = Deadline(LISTING_DEADLINE_S)
    try:
        found = await GitRepository.discover(folder, deadline)
        if folder == workspace:
            return RepositoryFolder(folder)
        if found is None:
            # Fails closed: the comparison answers NOT_A_REPOSITORY without looking again.
            return RepositoryFolder(folder, OUTSIDE_ANY_REPOSITORY)
        own = await GitRepository.discover(workspace, deadline)
    except GitError as error:
        # Fails closed as well: the comparison reports Git's problem, as for the workspace.
        return RepositoryFolder(folder, UnreadableRepository(error))
    places = _places(found)
    # A worktree of a repository larger than the workspace could read all its history.
    if own is not None and own.top_level.resolve() != workspace.resolve():
        raise _refuse(repository, "can't be compared in a workspace inside a larger repository")
    if own is not None and _places(own) == places:
        return RepositoryFolder(folder, found)
    git_directory, common_directory, top_level = places
    # Only a worktree registered in the workspace repository's own `worktrees` folder: a
    # `.git` file or `commondir` aimed at its metadata from anywhere else isn't one.
    own_common = own.common_directory.resolve() if own is not None else None
    if (
        own_common is None
        or common_directory != own_common
        or git_directory.parent != own_common / "worktrees"
    ):
        raise _refuse(repository, "isn't a worktree of the workspace's repository")
    if not _is_within(top_level, workspace):
        raise _refuse(repository, "has its working tree outside the workspace")
    if not await _registered_at(git_directory, top_level):
        raise _refuse(repository, "isn't a worktree of the workspace's repository")
    return RepositoryFolder(folder, found)
