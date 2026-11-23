"""The workspace API's file and text searches on the host.

SearchFiles answers in the REST file search's substring mode, and in fuzzy mode
where the host can rank paths. Candidates come from Git through the hardened
runner (tracked, and untracked unignored files) and, when ignored files are
wanted or there is no repository, from the REST search's budgeted walk.
SearchText runs the REST content search's ripgrep script and streams its
matches. Directory scopes are literal workspace paths matched by whole
components, never globs.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import functools
import heapq
import logging
import os
import re
import stat
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from google.protobuf.timestamp_pb2 import Timestamp

from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    ENTRY_TYPE_DIRECTORY,
    ENTRY_TYPE_FILE,
    FILE_SEARCH_MODE_FUZZY,
    FILE_SEARCH_MODE_SUBSTRING,
    FILE_SEARCH_MODE_UNSPECIFIED,
    SEARCH_ORDER_PRIORITY,
    Entry,
    FileSearchResult,
    SearchFilesRequest,
    SearchFilesResponse,
    SearchTextRequest,
    SearchTextResponse,
    TextSearchMatch,
)
from omnigent.entities.environment_filesystem import FilesystemEntry, InvalidPath
from omnigent.host import workspace_fuzzy
from omnigent.host.workspace_fuzzy import CorpusCache, PathCorpus, RankedPath
from omnigent.host.workspace_git.files import FileListing, workspace_files
from omnigent.host.workspace_git.runner import (
    Deadline,
    GitError,
    start_owned_process,
    trusted_search_path,
)
from omnigent.host.workspace_text_priority import SearchRun, beneath, priority_runs
from omnigent.inner.async_utils import run_sync_on_thread
from omnigent.runner.environment_filesystem import (
    _SEARCH_SCAN_BUDGET,
    _glob_to_regex,
    _lstat_beneath,
    _validate_path,
    describe_indexed_path,
    indexed_path_filter,
    merge_entries,
    paths_under,
    search_indexed_paths,
)
from omnigent.workspace_content_search import (
    MAX_MATCHES,
    MAX_QUERY_LENGTH,
    MAX_SCRIPT_OUTPUT,
    RIPGREP_TIME_LIMIT_S,
    SCRIPT_TIMEOUT_S,
    ContentSearchError,
    content_search_globs,
    content_search_script,
    literal_glob,
    parse_record,
)
from omnigent.workspace_file_search import walk_search

_logger = logging.getLogger(__name__)

_DEFAULT_FILE_RESULTS = 500
_MAX_FILE_RESULTS = 10_000
# Covers discovery and every listing; the walk has its own entry budget.
_FILE_SEARCH_DEADLINE_S = 20.0
# Matching lines per SearchText message.
_TEXT_MATCH_BATCH = 100
# Ranked paths beyond the limit, standing in for ones deleted since Git listed them.
_SPARE_RANKED = 32
# Fuzzy mode's file lists, reused while Git's listing is unchanged.
_corpora = CorpusCache()


def _validated(path: str, field: str) -> str:
    try:
        return _validate_path(path) if path else ""
    except InvalidPath as exc:
        raise ConnectError(Code.INVALID_ARGUMENT, f"{field}: {exc}") from exc


def _outermost(directories: Sequence[str]) -> tuple[str, ...]:
    """Drop directories inside another of ``directories``, and duplicates."""
    kept: list[str] = []
    for directory in sorted(set(directories), key=lambda d: (d.count("/"), d)):
        if not any(beneath(directory, outer) for outer in kept):
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
        if any(beneath(path, excluded) for excluded in self.excludes):
            return False
        return not self.includes or any(beneath(path, included) for included in self.includes)

    def may_enter(self, directory: str) -> bool:
        """Whether a walk must enter ``directory`` to reach in-scope results."""
        if any(beneath(directory, excluded) for excluded in self.excludes):
            return False
        return not self.includes or any(
            beneath(directory, included) or beneath(included, directory)
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


def _result(
    entry: FilesystemEntry, *, is_symlink: bool, ranked: RankedPath | None = None
) -> FileSearchResult:
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
        ),
        score=ranked.score if ranked else 0,
        matched_indices=ranked.matched_indices if ranked else (),
    )


def _globs(patterns: Sequence[str]) -> list[str]:
    return [pattern.strip() for pattern in patterns if pattern.strip()]


async def search_files(root: Path, request: SearchFilesRequest) -> SearchFilesResponse:
    """Answer a SearchFiles for the workspace at ``root``.

    Fuzzy mode, the default, ranks files best first, and lists them in path
    order for a blank query. It is served only where paths can be ranked.

    :raises ConnectError: UNIMPLEMENTED for a mode this host doesn't serve,
        INVALID_ARGUMENT for a bad path or scope, NOT_FOUND for a missing
        ``path``, UNAVAILABLE when Git can't list the tracked files.
    """
    fuzzy = request.mode in (FILE_SEARCH_MODE_UNSPECIFIED, FILE_SEARCH_MODE_FUZZY)
    if not (request.mode == FILE_SEARCH_MODE_SUBSTRING or (fuzzy and workspace_fuzzy.available())):
        raise ConnectError(Code.UNIMPLEMENTED, "Only substring file search is served here")
    base = _validated(request.path, "path")
    if not real_directory(root, base, "path"):
        raise ConnectError(Code.NOT_FOUND, f"Directory {base!r} not found")
    scope = DirectoryScope.parse(root, request.include_directories, request.exclude_directories)
    query = request.query.strip() if fuzzy else request.query.strip().lower()
    if fuzzy and len(query) > MAX_QUERY_LENGTH:
        raise ConnectError(
            Code.INVALID_ARGUMENT, f"Enter a query of at most {MAX_QUERY_LENGTH} characters"
        )
    if not query and not fuzzy:
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
    if fuzzy:
        return await _search_fuzzy(
            _FuzzySearch(
                root=root,
                base=base,
                scope=scope,
                query=query,
                limit=limit,
                include=include,
                exclude=exclude,
                include_git_ignored=request.include_git_ignored,
                listing=listing,
                in_scope=in_scope,
                describe_link=describe_link,
            )
        )
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


@dataclass(frozen=True)
class _FuzzySearch:
    """A validated fuzzy-mode SearchFiles; paths are relative to ``base``."""

    root: Path
    base: str
    scope: DirectoryScope
    query: str
    limit: int
    include: list[str]
    exclude: list[str]
    include_git_ignored: bool
    listing: FileListing | None
    in_scope: Callable[[str], bool]
    describe_link: Callable[[str, os.stat_result], FilesystemEntry]


def _git_corpora(search: _FuzzySearch) -> list[PathCorpus]:
    """Corpora of the tracked and untracked files beneath ``base``, in path order."""
    if search.listing is None:
        return []
    restricted = bool(
        search.include or search.exclude or search.scope.includes or search.scope.excludes
    )
    kept = (
        indexed_path_filter(search.include, search.exclude, search.in_scope)
        if restricted
        else None
    )

    def paths(source: list[str], tracked: list[str] | None = None) -> list[str]:
        beneath = paths_under(source, search.base)
        if tracked is not None:
            # A file staged since the untracked list was read is in both lists.
            beneath = [path for path in beneath if not _listed(tracked, prefix + path)]
        return sorted(filter(kept, beneath) if kept else beneath)

    prefix = f"{search.base}/" if search.base else ""
    filters = (search.base, tuple(search.include), tuple(search.exclude), search.scope)
    tracked, untracked = search.listing.tracked, search.listing.untracked
    corpora = []
    if tracked:
        key = (search.root, "tracked", filters)
        corpora.append(_corpora.corpus(key, (tracked,), functools.partial(paths, tracked)))
    if untracked:
        key = (search.root, "untracked", filters)
        corpora.append(
            _corpora.corpus(
                key, (tracked, untracked), functools.partial(paths, untracked, tracked)
            )
        )
    return corpora


def _listed(paths: list[str], path: str) -> bool:
    """Whether ``path`` is in ``paths``, which is in path order."""
    index = bisect.bisect_left(paths, path)
    return index < len(paths) and paths[index] == path


def _first_files(
    paths: Iterable[str], limit: int, describe: Callable[[str], FilesystemEntry | None]
) -> tuple[list[FilesystemEntry], bool]:
    """Describe ``paths`` in order until ``limit`` files; also whether more remain.

    Paths deleted since they were listed, and directories, are skipped.
    """
    entries: list[FilesystemEntry] = []
    for path in paths:
        entry = describe(path)
        if entry is None or entry.type != "file":
            continue
        if len(entries) == limit:
            return entries, True
        entries.append(entry)
    return entries, False


async def _search_fuzzy(search: _FuzzySearch) -> SearchFilesResponse:
    """Rank files against the query, or list them in path order for a blank one."""
    search_root = search.root / search.base
    prefix = f"{search.base}/" if search.base else ""
    corpora = await run_sync_on_thread(_git_corpora, search)
    indexed = sum(len(corpus) for corpus in corpora)
    walked: dict[str, FilesystemEntry] = {}
    walk_truncated = False
    if search.listing is None or search.include_git_ignored:
        found, walk_truncated = await run_sync_on_thread(
            walk_search,
            search_root,
            "",
            [re.compile(_glob_to_regex(p), re.IGNORECASE) for p in search.include],
            [re.compile(_glob_to_regex(p), re.IGNORECASE) for p in search.exclude],
            # Above the budget, so a walk that stops early says so.
            _SEARCH_SCAN_BUDGET + 1,
            budget=_SEARCH_SCAN_BUDGET,
            descend=lambda directory: search.scope.may_enter(prefix + directory),
            emit=search.in_scope,
            describe_link=search.describe_link,
        )
        listed = {path for corpus in corpora for path in corpus.paths}
        walked = {e.path: e for e in found if e.type == "file" and e.path not in listed}
        if walked:
            corpora.append(await run_sync_on_thread(PathCorpus, sorted(walked)))

    def describe(path: str) -> FilesystemEntry | None:
        return walked.get(path) or describe_indexed_path(
            search_root, path, describe_link=search.describe_link
        )

    ranked: dict[str, RankedPath] = {}
    if search.query:
        wanted = search.limit + _SPARE_RANKED
        while True:
            hits, more = await run_sync_on_thread(
                workspace_fuzzy.rank, corpora, search.query, wanted
            )
            ranked = {hit.path: hit for hit in hits}
            entries, truncated = await run_sync_on_thread(
                _first_files, ranked, search.limit, describe
            )
            # Matches deleted since Git listed them neither fill the limit nor say more
            # matched; rank further until a live file past the limit does, or none are left.
            if truncated or not more:
                break
            wanted *= 4
    else:
        ordered = heapq.merge(*(corpus.paths for corpus in corpora))
        entries, truncated = await run_sync_on_thread(
            _first_files, ordered, search.limit, describe
        )
    links = await run_sync_on_thread(_symlinks, search_root, [e.path for e in entries])
    listing = search.listing
    return SearchFilesResponse(
        results=[
            _result(entry, is_symlink=entry.path in links, ranked=ranked.get(entry.path))
            for entry in entries
        ],
        truncated=truncated,
        indexed_paths=indexed,
        index_incomplete=walk_truncated or (listing is not None and listing.incomplete),
        untracked_unavailable=listing is not None and listing.untracked is None,
    )


def text_search_arguments(
    root: Path, request: SearchTextRequest
) -> tuple[list[str], list[str]] | None:
    """The ripgrep options and start paths for ``request``'s scopes and flags.

    Includes become explicit start paths, which ripgrep would follow through a
    link or read as a special file, so each must be a real directory. Excludes
    become anchored, escaped globs placed after the user's, so they win.

    :returns: Options and start paths, or ``None`` when no include exists.
    :raises ConnectError: INVALID_ARGUMENT for a scope outside the workspace, or
        an include that is a link, a file or a special file.
    """
    included = _outermost(
        [_validated(d, "include_directories") for d in request.include_directories]
    )
    excluded = _outermost(
        [
            d
            for d in (_validated(d, "exclude_directories") for d in request.exclude_directories)
            if d
        ]
    )
    if "" in included:
        included = ()
    present = [d for d in included if real_directory(root, d, "include_directories")]
    if included and not present:
        return None
    options: list[str] = []
    if request.whole_word:
        options.append("-w")
    if request.include_git_ignored:
        options.append("--no-ignore-vcs")
    for directory in excluded:
        literal = literal_glob(directory)
        options += [f"--glob=!/{literal}", f"--glob=!/{literal}/**"]
    return options, [f"./{d}" for d in present] or ["."]


async def _content_search_records(
    root: Path, script: str, timeout: float = SCRIPT_TIMEOUT_S
) -> AsyncGenerator[dict[str, object]]:
    """Run the content search script in ``root``, yielding its records.

    Closing the iterator early kills and reaps the script and ripgrep.

    :param timeout: Seconds the reads may take in all.

    :raises ContentSearchError: For ripgrep's own refusals, such as a bad regex.
    :raises ConnectError: UNAVAILABLE when the search couldn't run or finish.
    """
    # Isolated (-I), as the runner launches it, so workspace files can't stand in
    # for the script's imports. Its own session lets one signal stop ripgrep too.
    process = await start_owned_process(
        [sys.executable, "-I", "-u", "-c", script],
        cwd=root,
        env={**os.environ, "PATH": trusted_search_path()},
        pipe_stderr=False,
        limit=MAX_SCRIPT_OUTPUT,
    )
    received = 0
    loop = asyncio.get_running_loop()
    # Only the reads count: a client slow to take the matches doesn't end the search.
    remaining = float(timeout)
    try:
        while True:
            started = loop.time()
            async with asyncio.timeout(remaining):
                line = await process.stdout.readline()
            remaining -= loop.time() - started
            if not line:
                raise ValueError("Content search ended without a summary")
            received += len(line)
            if received > MAX_SCRIPT_OUTPUT:
                raise ValueError("Content search output passed its limit")
            record = parse_record(line)
            if "error" in record:
                raise ContentSearchError(str(record["error"]))
            yield record
            if "truncated" in record:
                return
    except (TimeoutError, ValueError) as exc:
        _logger.warning("Workspace content search failed: %s", exc)
        raise ConnectError(
            Code.UNAVAILABLE, "Content search could not run in this workspace"
        ) from exc
    finally:
        await process.close()


def _skip_globs(run: SearchRun) -> list[str]:
    """Anchored, escaped excludes for what earlier runs of a search searched."""
    files = [f"--glob=!/{literal_glob(path)}" for path in run.skip_files]
    folders = [
        glob
        for directory in run.skip_directories
        for glob in (
            f"--glob=!/{literal_glob(directory)}",
            f"--glob=!/{literal_glob(directory)}/**",
        )
    ]
    return [*files, *folders]


class _SearchBudget:
    """Seconds a search spends waiting for its passes and their matches, out of ``seconds``.

    The time a client takes to read the matches doesn't count.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._used = 0.0
        self._since: float | None = None

    async def timed[T](self, waiting: Awaitable[T]) -> T:
        self._since = time.monotonic()
        try:
            return await waiting
        finally:
            self._used += time.monotonic() - self._since
            self._since = None

    def left(self) -> float:
        """Seconds left, counting a wait still under way."""
        waiting = time.monotonic() - self._since if self._since is not None else 0.0
        return self.seconds - self._used - waiting


async def _walk_run(start_paths: list[str]) -> AsyncGenerator[SearchRun]:
    yield SearchRun(start_paths, [], [])


async def search_text(
    root: Path, request: SearchTextRequest
) -> AsyncGenerator[SearchTextResponse]:
    """Stream a SearchText's matches in batches, then a final ``done`` message.

    In priority order (feature ``text_search_priority``), focused files come
    first, then their folders, then the folders with the most changes against
    ``changed_against``, then the rest of the scope. Each file is searched once,
    and every pass shares the result cap and the time limit.
    """
    arguments = text_search_arguments(root, request)
    if arguments is None:
        yield SearchTextResponse(done=True)
        return
    options, start_paths = arguments
    limit = min(request.maximum_results or MAX_MATCHES, MAX_MATCHES)

    def script(run: SearchRun, remaining: int, time_limit: float = RIPGREP_TIME_LIMIT_S) -> str:
        return content_search_script(
            request.query,
            case_sensitive=request.case_sensitive,
            regex=request.regular_expression,
            include=request.include,
            exclude=request.exclude,
            limit=remaining,
            extra_args=[*options, *_skip_globs(run), *(["--threads=1"] if run.in_order else [])],
            search_paths=run.start_paths,
            time_limit=time_limit,
        )

    budget = _SearchBudget(RIPGREP_TIME_LIMIT_S)
    try:
        # Invalid queries and filters are refused before anything runs.
        script(SearchRun(start_paths, [], []), limit)
        if request.order == SEARCH_ORDER_PRIORITY:
            scope = DirectoryScope.parse(
                root, request.include_directories, request.exclude_directories
            )
            walk_options = [
                *content_search_globs(request.include, request.exclude),
                "--glob=!.git",
                *options,
            ]
            runs = priority_runs(
                root,
                request,
                start_paths=start_paths,
                in_scope=scope.allows,
                walk_options=walk_options,
                time_left=budget.left,
            )
        else:
            runs = _walk_run(start_paths)
        batch: list[TextSearchMatch] = []
        found = 0
        truncated = False
        warnings: list[str] = []
        async with contextlib.aclosing(runs) as passes:
            while (run := await budget.timed(anext(passes, None))) is not None:
                left = budget.left()
                if found == limit or left <= 0:
                    truncated = True  # later passes may match more
                    break
                # Each script stops its ripgrep with time left to report the results cut short.
                records = _content_search_records(
                    root,
                    script(run, limit - found, left),
                    SCRIPT_TIMEOUT_S - RIPGREP_TIME_LIMIT_S + left,
                )
                async with contextlib.aclosing(records) as stream:
                    while (record := await budget.timed(anext(stream, None))) is not None:
                        if "match" not in record:
                            # Files a pass couldn't read leave its results incomplete, but
                            # later passes still run while results and time are left.
                            truncated = truncated or bool(record["truncated"])
                            note = str(record["warning"] or "")
                            if note and note not in warnings:
                                warnings.append(note)
                            break
                        batch.append(TextSearchMatch(**record["match"]))  # type: ignore[arg-type]
                        found += 1
                        if len(batch) == _TEXT_MATCH_BATCH:
                            yield SearchTextResponse(matches=batch)
                            batch = []
                if batch and request.order == SEARCH_ORDER_PRIORITY:
                    # A pass's matches go out before the next pass starts.
                    yield SearchTextResponse(matches=batch)
                    batch = []
        yield SearchTextResponse(
            matches=batch, done=True, truncated=truncated, warning=" ".join(warnings)
        )
    except ContentSearchError as exc:
        raise ConnectError(Code.INVALID_ARGUMENT, exc.message) from exc
