"""The order a priority SearchText searches in: focused files, their folders,
the folders with the most changes, then the rest of the scope.

Each pass is its own ripgrep run, so results arrive in that order, and later
passes leave out what earlier ones searched. A file or folder is searched
early only if a full walk of the scope would reach it.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from connectrpc.errors import ConnectError

from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    COMPARISON_KIND_LAST_COMMIT,
    COMPARISON_KIND_UNSPECIFIED,
    Comparison,
    ComparisonKind,
    ListWorkingCopyChangesRequest,
    SearchTextRequest,
)
from omnigent.entities.environment_filesystem import InvalidPath
from omnigent.host.workspace_git.runner import GitError, start_owned_process, trusted_search_path
from omnigent.host.workspace_git.working_copy import list_working_copy_changes
from omnigent.inner.async_utils import run_sync_on_thread
from omnigent.runner.environment_filesystem import (
    _lstat_beneath,
    _validate_path,
    read_regular_file_beneath,
)
from omnigent.workspace_content_search import literal_glob

MAX_FOCUSED_FILES = 16
MAX_CHANGED_DIRECTORIES = 16
# How long a search waits for the change list before skipping the changed folders;
# a slow list keeps building for the next search.
_CHANGES_WAIT_S = 0.3
# A change list is reused this long.
_CHANGES_REUSE_S = 30.0
_MAX_CACHED_CHANGES = 16
_LIST_FILES_BYTES = 4 * 1024 * 1024
_REACH_DEADLINE_S = 5.0
# The check's globs grow with the square of a folder's depth; past this many bytes
# it isn't run, well inside every platform's argument limit.
_MAX_REACH_GLOB_BYTES = 256 * 1024
# A focused file is searched first only if this much of it, or all, is text.
_TEXT_CHECK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SearchRun:
    """One ripgrep run of a priority search.

    :param start_paths: Where it starts, ``./``-prefixed or ``.``, in this order.
    :param skip_files: Workspace files an earlier run searched.
    :param skip_directories: Workspace folders an earlier run searched.
    :param in_order: Search the start paths one at a time, keeping their order.
    """

    start_paths: list[str]
    skip_files: list[str]
    skip_directories: list[str]
    in_order: bool = False


def beneath(path: str, directory: str) -> bool:
    """Whether ``path`` is ``directory`` or inside it, by whole components; all are in ``""``."""
    return not directory or path == directory or path.startswith(directory + "/")


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def ordered_directories(directories: Sequence[str], earlier: Sequence[str]) -> list[str]:
    """Keep ``directories`` in order, without the root or any at or beneath an earlier one."""
    kept: list[str] = []
    for directory in directories:
        if directory and not any(beneath(directory, listed) for listed in [*earlier, *kept]):
            kept.append(directory)
    return kept


def weighted_change_directories(changes: Sequence[tuple[str, int]]) -> list[str]:
    """The folders with the most changes, heaviest first.

    Each changed file weighs its added and removed lines plus one, on its folder.
    The workspace root, and a folder beneath one already listed, are left out.

    :param changes: Changed files' workspace paths and changed line counts.
    """
    weights: dict[str, int] = {}
    for path, lines in changes:
        parent = _parent(path)
        if parent:
            weights[parent] = weights.get(parent, 0) + lines + 1
    ordered = sorted(weights, key=lambda directory: (-weights[directory], directory))
    return ordered_directories(ordered, [])[:MAX_CHANGED_DIRECTORIES]


async def _changed_directories(root: Path, kind: ComparisonKind) -> list[str] | None:
    try:
        response = await list_working_copy_changes(
            root, ListWorkingCopyChangesRequest(comparison=Comparison(kind=kind))
        )
    except (GitError, ConnectError, OSError):  # only skips the changed folders
        return None
    if response.base.HasField("unavailable"):
        return None
    return weighted_change_directories(
        [(changed.path, changed.lines_added + changed.lines_removed) for changed in response.files]
    )


_ChangesTask = asyncio.Task[list[str] | None]
_changes: OrderedDict[tuple[int, Path, int], tuple[float, _ChangesTask]] = OrderedDict()


def _changes_task(root: Path, kind: ComparisonKind) -> _ChangesTask:
    """The change list for ``root``, reused briefly, built at most once at a time."""
    key = (id(asyncio.get_running_loop()), root, kind)
    cached = _changes.pop(key, None)
    if cached is None or time.monotonic() - cached[0] > _CHANGES_REUSE_S:
        cached = (time.monotonic(), asyncio.ensure_future(_changed_directories(root, kind)))
    _changes[key] = cached
    while len(_changes) > _MAX_CACHED_CHANGES:
        _changes.popitem(last=False)
    return cached[1]


def _start_of(path: str, start_paths: Sequence[str]) -> str | None:
    """The walk's start folder that ``path`` is at or beneath, ``""`` for the root."""
    starts = ["" if start == "." else start.removeprefix("./") for start in start_paths]
    containing = [start for start in starts if beneath(path, start)]
    return max(containing, key=len) if containing else None


def _only_towards(parent: str, name: str) -> list[str]:
    """Globs that leave ``name`` the only entry of ``parent`` a walk enters."""
    base = f"/{literal_glob(parent)}/" if parent else "/"
    # Every other name is longer, a prefix of it, or differs at some character.
    globs = [f"--glob=!{base}{literal_glob(name)}?*"]
    for index, character in enumerate(name):
        before = literal_glob(name[:index])
        member = character if character.isalnum() else f"\\{character}"
        globs.append(f"--glob=!{base}{before}[!{member}]*")
        if index:
            globs.append(f"--glob=!{base}{before}")
    return globs


async def _reached_files(
    root: Path,
    directory: str,
    start_paths: Sequence[str],
    options: Sequence[str],
    time_left: Callable[[], float],
) -> set[str]:
    """The files directly in ``directory`` that the walk, with ``options``, reaches.

    It walks from the start above ``directory`` through only the folders on the way,
    so each of them is pruned, or not, by the same globs and ignore files as in the walk.

    :param time_left: Seconds the search has left; the walk takes no longer.
    """
    start = _start_of(directory, start_paths)
    deadline = min(_REACH_DEADLINE_S, time_left())
    if start is None or deadline <= 0:
        return set()
    names = directory[len(start) :].strip("/").split("/") if directory != start else []
    globs: list[str] = []
    parent = start
    for name in names:
        globs += _only_towards(parent, name)
        parent = f"{parent}/{name}" if parent else name
    if sum(len(glob.encode()) for glob in globs) > _MAX_REACH_GLOB_BYTES:
        return set()  # unsure, so none of them is searched early
    try:
        process = await start_owned_process(
            [
                "rg",
                "--files",
                "--null",  # a file name may hold a newline
                "--no-config",
                "--hidden",
                *options,
                *globs,  # later globs win, so the walk stays on the way
                "--max-depth",
                str(len(names) + 1),
                "--",
                f"./{start}" if start else ".",
            ],
            cwd=root,
            env={**os.environ, "PATH": trusted_search_path()},
            pipe_stderr=False,
        )
    except OSError:  # e.g. ripgrep missing or arguments too long: search nothing early
        return set()
    chunks: list[bytes] = []
    received = 0
    try:
        async with asyncio.timeout(deadline):
            while received < _LIST_FILES_BYTES and (chunk := await process.stdout.read(65536)):
                chunks.append(chunk)
                received += len(chunk)
    except TimeoutError:
        chunks = []  # unsure, so none of them is searched early
    finally:
        await process.close()
    # Each name ends in a NUL, so one cut off at the byte cap is left out.
    records = b"".join(chunks)[:_LIST_FILES_BYTES].split(b"\0")[:-1]
    paths = (os.fsdecode(record).removeprefix("./") for record in records)
    return {path for path in paths if path and _parent(path) == directory}


async def _reached_in(
    root: Path,
    directories: Sequence[str],
    start_paths: Sequence[str],
    options: Sequence[str],
    time_left: Callable[[], float],
) -> dict[str, set[str]]:
    found = await asyncio.gather(
        *(
            _reached_files(root, directory, start_paths, options, time_left)
            for directory in directories
        )
    )
    return dict(zip(directories, found, strict=True))


def _text_files(root: Path, paths: Sequence[str]) -> list[str]:
    """The regular files of ``paths`` that hold no NUL byte, checked whole.

    Ripgrep searches a file it's given by name even past a NUL, but a walk
    skips such a binary file, so only text is searched ahead of the walk.
    """
    resolved = root.resolve()
    kept: list[str] = []
    for path in paths:
        content = read_regular_file_beneath(resolved, path, _TEXT_CHECK_BYTES)
        if content is not None and len(content) <= _TEXT_CHECK_BYTES and b"\0" not in content:
            kept.append(path)
    return kept


def _real_directory(root: Path, path: str) -> bool:
    found = _lstat_beneath(root, path)
    return found is not None and stat.S_ISDIR(found.st_mode)


def _is_git_path(path: str) -> bool:
    return ".git" in path.split("/")


async def reachable_focused_files(
    root: Path,
    request: SearchTextRequest,
    *,
    start_paths: Sequence[str],
    in_scope: Callable[[str], bool],
    walk_options: Sequence[str],
    time_left: Callable[[], float],
) -> list[str]:
    """The request's focused files a full walk would search, in the order given.

    :param start_paths: Where the walk starts, ``./``-prefixed or ``.``.
    :param in_scope: Whether a workspace path is inside the search's directory scope.
    :param walk_options: The walk's ripgrep options: the user's globs, then the search's.
    :param time_left: Seconds the search has left, which bounds the check.
    """
    candidates: list[str] = []
    for raw in request.focused_paths[:MAX_FOCUSED_FILES]:
        try:
            path = _validate_path(raw) if raw else ""
        except InvalidPath:
            continue
        if path and path not in candidates and not _is_git_path(path) and in_scope(path):
            candidates.append(path)
    candidates = await run_sync_on_thread(_text_files, root, candidates)
    if not candidates:
        return []
    reached = await _reached_in(
        root, sorted({_parent(path) for path in candidates}), start_paths, walk_options, time_left
    )
    return [path for path in candidates if path in reached[_parent(path)]]


async def priority_runs(
    root: Path,
    request: SearchTextRequest,
    *,
    start_paths: Sequence[str],
    in_scope: Callable[[str], bool],
    walk_options: Sequence[str],
    time_left: Callable[[], float],
) -> AsyncGenerator[SearchRun]:
    """The runs of a priority search, each computed once the previous one finished.

    :param start_paths: Where the whole-scope walk starts, ``./``-prefixed or ``.``.
    :param in_scope: Whether a workspace path is inside the search's directory scope.
    :param walk_options: The walk's ripgrep options, to decide what it would reach.
    :param time_left: Seconds the search has left; preparing a run waits no longer.
    """
    focused = await reachable_focused_files(
        root,
        request,
        start_paths=start_paths,
        in_scope=in_scope,
        walk_options=walk_options,
        time_left=time_left,
    )
    # Started after the focused files are checked, so the two don't compete for Git's index.
    kind = request.changed_against
    changes = _changes_task(
        root, kind if kind != COMPARISON_KIND_UNSPECIFIED else COMPARISON_KIND_LAST_COMMIT
    )
    searched: list[str] = []
    if focused:
        yield SearchRun([f"./{path}" for path in focused], [], [], in_order=True)
    for directory in ordered_directories([_parent(path) for path in focused], []):
        yield SearchRun([f"./{directory}"], focused, list(searched))
        searched.append(directory)

    done, _ = await asyncio.wait({changes}, timeout=max(0.0, min(_CHANGES_WAIT_S, time_left())))
    changed = (changes.result() if done else None) or []
    candidates = [
        directory
        for directory in ordered_directories(changed, searched)
        if in_scope(directory) and not _is_git_path(directory) and _real_directory(root, directory)
    ]
    reached = await _reached_in(root, candidates, start_paths, walk_options, time_left)
    for directory in candidates:
        # Searched early only where the walk reaches files of its own.
        if not reached[directory]:
            continue
        yield SearchRun([f"./{directory}"], focused, list(searched))
        searched.append(directory)

    # A start beneath a searched folder is dropped: a walk doesn't prune its own start.
    rest = [
        start
        for start in start_paths
        if start == "." or not any(beneath(start[2:], directory) for directory in searched)
    ]
    if rest:
        yield SearchRun(list(rest), focused, list(searched))
