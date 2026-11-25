"""The folder a Git request compares: the workspace, or a repository inside it.

A request may name a directory relative to the workspace root, such as a worktree
checked out inside it. It is confined the way file reads are. Its repository must be
the workspace's own or be stored inside the workspace, and its working tree must lie
inside the workspace unless it is the workspace's own, so a crafted link, ``.git`` file
or ``core.worktree`` can't point a comparison at another place on the host.
"""

from __future__ import annotations

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
)
from omnigent.workspace_fs import WorkspaceReader, WorkspaceReaderError

# Two rev-parse calls at most; seconds means something is wrong.
_CHECK_DEADLINE_S = 5.0


@dataclass(frozen=True)
class RepositoryFolder:
    """A checked folder, and the repository found there when one was checked.

    :param path: The workspace root, or the resolved directory inside it.
    :param repository: The repository the checks passed for, which the Git calls reuse
        rather than looking it up again; ``OUTSIDE_ANY_REPOSITORY`` for a folder outside
        any, which they answer as not a repository; ``None`` for the workspace root, which
        they discover as before.
    """

    path: Path
    repository: CheckedRepository = None


def _refuse(repository: str, reason: str) -> ConnectError:
    # Names only what the caller sent, never a resolved path.
    return ConnectError(Code.INVALID_ARGUMENT, f"Repository {repository!r} {reason}")


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


async def repository_folder(reader: WorkspaceReader, repository: str) -> RepositoryFolder:
    """The folder a Git request named by ``repository`` compares, once checked.

    :param reader: The reader for the workspace the call may read.
    :param repository: A directory relative to the workspace root; empty for the root.
    :returns: The workspace root, or the resolved directory inside it with its repository.
    :raises ConnectError: INVALID_ARGUMENT when it escapes the workspace, even through a
        symlink, isn't a folder, lies in a Git directory, belongs to a repository stored
        outside the workspace that isn't the workspace's own, or has its working tree
        outside the workspace without being the workspace's own repository.
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
    deadline = Deadline(_CHECK_DEADLINE_S)
    try:
        found = await GitRepository.discover(folder, deadline)
        if folder == workspace:
            return RepositoryFolder(folder)
        if found is None:
            # Fails closed: the comparison answers NOT_A_REPOSITORY without looking again.
            return RepositoryFolder(folder, OUTSIDE_ANY_REPOSITORY)
        common = found.common_directory.resolve()
        top_level = found.top_level.resolve()
        stored_inside = _is_within(common, workspace)
        tree_inside = _is_within(top_level, workspace)
        own = (
            None
            if stored_inside and tree_inside
            else await GitRepository.discover(workspace, deadline)
        )
    except GitError as error:
        raise _refuse(repository, "couldn't be read with Git") from error
    same_store = own is not None and own.common_directory.resolve() == common
    own_whole = same_store and own is not None and own.top_level.resolve() == top_level
    # A working tree outside the workspace only as the workspace's own repository, whole:
    # a nested `.git` aimed by core.worktree at the repository around the workspace isn't.
    if not tree_inside and not own_whole:
        raise _refuse(repository, "has its working tree outside the workspace")
    if not stored_inside and not same_store:
        raise _refuse(repository, "belongs to a repository outside the workspace")
    return RepositoryFolder(folder, found)
