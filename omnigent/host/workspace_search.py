"""The workspace API's file search on the host, in the REST file search's substring mode.

Candidates come from Git through the hardened runner (tracked, and untracked
unignored files) and, when ignored files are wanted or there is no repository,
from the REST search's budgeted walk. Directory scopes are literal workspace
paths matched by whole components, never globs.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from google.protobuf.timestamp_pb2 import Timestamp

from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    ENTRY_TYPE_DIRECTORY,
    ENTRY_TYPE_FILE,
    FILE_SEARCH_MODE_SUBSTRING,
    Entry,
    FileSearchResult,
    SearchFilesRequest,
    SearchFilesResponse,
)
from omnigent.entities.environment_filesystem import FilesystemEntry, InvalidPath
from omnigent.host.workspace_git.files import workspace_files
from omnigent.host.workspace_git.runner import Deadline, GitError
from omnigent.inner.async_utils import run_sync_on_thread
from omnigent.runner.environment_filesystem import (
    _SEARCH_SCAN_BUDGET,
    _glob_to_regex,
    _lstat_beneath,
    _validate_path,
    merge_entries,
    paths_under,
    search_indexed_paths,
)
from omnigent.workspace_file_search import walk_search

_DEFAULT_FILE_RESULTS = 500
_MAX_FILE_RESULTS = 10_000
# Covers discovery and every listing; the walk has its own entry budget.
_FILE_SEARCH_DEADLINE_S = 20.0


def _beneath(path: str, directory: str) -> bool:
    """Whether ``path`` is ``directory`` or inside it, by whole components."""
    return not directory or path == directory or path.startswith(directory + "/")


def _validated(path: str, field: str) -> str:
    try:
        return _validate_path(path) if path else ""
    except InvalidPath as exc:
        raise ConnectError(Code.INVALID_ARGUMENT, f"{field}: {exc}") from exc


def _outermost(directories: Sequence[str]) -> tuple[str, ...]:
    """Drop directories inside another of ``directories``, and duplicates."""
    kept: list[str] = []
    for directory in sorted(set(directories), key=lambda d: (d.count("/"), d)):
        if not any(_beneath(directory, outer) for outer in kept):
            kept.append(directory)
    return tuple(sorted(kept))


def real_directory(root: Path, path: str, field: str) -> bool:
    """Check that ``path`` is a directory reached without symlinks.

    :returns: False when nothing is there.
    :raises ConnectError: INVALID_ARGUMENT for a symlink, a file or a special file.
    """
    if not path:
        return True
    found = _lstat_beneath(root, path)
    if found is None:
        if os.path.lexists(root / path):
            raise ConnectError(
                Code.INVALID_ARGUMENT, f"{field}: {path!r} is reached through a link"
            )
        return False
    if not stat.S_ISDIR(found.st_mode):
        raise ConnectError(Code.INVALID_ARGUMENT, f"{field}: {path!r} is not a directory")
    return True


@dataclass(frozen=True)
class DirectoryScope:
    """Workspace-relative directories a search keeps to.

    :param includes: Results come only from beneath one, when any.
    :param excludes: Results never come from beneath one; they win over includes.
    """

    includes: tuple[str, ...]
    excludes: tuple[str, ...]

    @classmethod
    def parse(cls, root: Path, includes: Sequence[str], excludes: Sequence[str]) -> DirectoryScope:
        """Validate and de-duplicate the request's scopes.

        An include must be a real directory, or missing; an exclude only a valid path.
        """
        included = [_validated(d, "include_directories") for d in includes]
        excluded = [_validated(d, "exclude_directories") for d in excludes]
        for directory in included:
            real_directory(root, directory, "include_directories")
        # An empty include is the whole workspace, so it removes the restriction.
        unrestricted = "" in included
        return cls(
            includes=() if unrestricted else _outermost(included),
            excludes=_outermost([d for d in excluded if d]),
        )

    def allows(self, path: str) -> bool:
        """Whether a result at ``path`` is in scope."""
        if any(_beneath(path, excluded) for excluded in self.excludes):
            return False
        return not self.includes or any(_beneath(path, included) for included in self.includes)

    def may_enter(self, directory: str) -> bool:
        """Whether a walk must enter ``directory`` to reach in-scope results."""
        if any(_beneath(directory, excluded) for excluded in self.excludes):
            return False
        return not self.includes or any(
            _beneath(directory, included) or _beneath(included, directory)
            for included in self.includes
        )


def _link_entry(workspace: Path, location: str, path: str, own: os.stat_result) -> FilesystemEntry:
    """Describe a listed symlink by its target when that stays in the workspace.

    A target outside it, or a dangling link, is a file of unknown size.

    :param workspace: The workspace directory, resolved.
    :param location: The link's path relative to ``workspace``.
    :param path: The path the result reports.
    :param own: The link's own ``lstat``.
    """
    target: os.stat_result | None = None
    try:
        resolved = (workspace / location).resolve(strict=True)
    except (OSError, RuntimeError):
        resolved = None
    if resolved is not None and resolved.is_relative_to(workspace) and resolved != workspace:
        target = _lstat_beneath(workspace, resolved.relative_to(workspace).as_posix())
    is_dir = target is not None and stat.S_ISDIR(target.st_mode)
    described = target or own
    return FilesystemEntry(
        id=path,
        name=path.rsplit("/", 1)[-1],
        path=path,
        type="directory" if is_dir else "file",
        bytes=None if is_dir or target is None else described.st_size,
        modified_at=int(described.st_mtime),
    )


def _symlinks(root: Path, paths: Sequence[str]) -> set[str]:
    return {path for path in paths if (root / path).is_symlink()}


def _result(entry: FilesystemEntry, *, is_symlink: bool) -> FileSearchResult:
    return FileSearchResult(
        entry=Entry(
            name=entry.name,
            path=entry.path,
            type=ENTRY_TYPE_DIRECTORY if entry.type == "directory" else ENTRY_TYPE_FILE,
            size=entry.bytes,
            modify_time=None
            if entry.modified_at is None
            else Timestamp(seconds=entry.modified_at),
            is_symlink=is_symlink,
        )
    )


def _globs(patterns: Sequence[str]) -> list[str]:
    return [pattern.strip() for pattern in patterns if pattern.strip()]


async def search_files(root: Path, request: SearchFilesRequest) -> SearchFilesResponse:
    """Answer a substring-mode SearchFiles for the workspace at ``root``.

    :raises ConnectError: UNIMPLEMENTED for fuzzy mode, INVALID_ARGUMENT for a
        bad path or scope, NOT_FOUND for a missing ``path``, UNAVAILABLE when
        Git can't list the tracked files.
    """
    if request.mode != FILE_SEARCH_MODE_SUBSTRING:
        raise ConnectError(Code.UNIMPLEMENTED, "Only substring file search is served here")
    base = _validated(request.path, "path")
    if not real_directory(root, base, "path"):
        raise ConnectError(Code.NOT_FOUND, f"Directory {base!r} not found")
    scope = DirectoryScope.parse(root, request.include_directories, request.exclude_directories)
    query = request.query.strip().lower()
    if not query:
        return SearchFilesResponse()
    limit = min(request.maximum_results or _DEFAULT_FILE_RESULTS, _MAX_FILE_RESULTS)
    include, exclude = _globs(request.include), _globs(request.exclude)
    prefix = f"{base}/" if base else ""
    search_root = root / base
    workspace = root.resolve()

    def in_scope(path: str) -> bool:
        return scope.allows(prefix + path)

    def describe_link(path: str, own: os.stat_result) -> FilesystemEntry:
        # Listed or walked, a link is described the same way, never by an outside target.
        return _link_entry(workspace, prefix + path, path, own)

    try:
        listing = await workspace_files(root).list_files(
            Deadline(_FILE_SEARCH_DEADLINE_S), retry_untracked=request.retry_untracked
        )
    except GitError as exc:
        raise ConnectError(Code.UNAVAILABLE, f"Git couldn't list the workspace: {exc}") from exc
    candidates: list[str] = []
    if listing is not None:
        candidates = paths_under([*listing.tracked, *(listing.untracked or [])], base)
    # One more than the limit says whether more matched.
    indexed = await run_sync_on_thread(
        search_indexed_paths,
        search_root,
        candidates,
        query,
        include=include,
        exclude=exclude,
        limit=limit + 1,
        kept_path=in_scope,
        describe_link=describe_link,
    )
    walked: list[FilesystemEntry] = []
    walk_truncated = False
    if listing is None or request.include_git_ignored:
        walked, walk_truncated = await run_sync_on_thread(
            walk_search,
            search_root,
            query,
            [re.compile(_glob_to_regex(p), re.IGNORECASE) for p in include],
            [re.compile(_glob_to_regex(p), re.IGNORECASE) for p in exclude],
            limit + 1,
            budget=_SEARCH_SCAN_BUDGET,
            descend=lambda directory: scope.may_enter(prefix + directory),
            emit=in_scope,
            describe_link=describe_link,
        )
    merged = merge_entries(indexed, walked, limit + 1)
    returned = merged[:limit]
    links = await run_sync_on_thread(_symlinks, search_root, [e.path for e in returned])
    return SearchFilesResponse(
        results=[_result(entry, is_symlink=entry.path in links) for entry in returned],
        truncated=len(merged) > limit,
        indexed_paths=len(candidates),
        index_incomplete=walk_truncated or (listing is not None and listing.incomplete),
        untracked_unavailable=listing is not None and listing.untracked is None,
    )
