"""The working copy's changes against a base commit: HEAD, or the default branch's branch point.

Everything goes through :class:`GitRepository`, so nothing here writes to the repository or
fetches. Comparisons of revisions a person typed build on these helpers in ``comparison``.
"""

from __future__ import annotations

import asyncio
import stat
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from connectrpc.code import Code
from connectrpc.errors import ConnectError

from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    CHANGE_STATUS_ADDED,
    CHANGE_STATUS_CONFLICTED,
    CHANGE_STATUS_DELETED,
    CHANGE_STATUS_MODIFIED,
    CHANGE_STATUS_TYPE_CHANGED,
    CHANGED_FILE_PROBLEM_FILTERED,
    CHANGED_FILE_PROBLEM_MISSING_OBJECT,
    CHANGED_FILE_PROBLEM_UNSPECIFIED,
    COMPARISON_FIELD_UNSPECIFIED,
    COMPARISON_KIND_BRANCH_POINT,
    COMPARISON_KIND_LAST_COMMIT,
    COMPARISON_KIND_REVISIONS,
    COMPARISON_KIND_UNSPECIFIED,
    COMPARISON_PROBLEM_GIT_FAILED,
    COMPARISON_PROBLEM_GIT_UNAVAILABLE,
    COMPARISON_PROBLEM_INVALID_REVISION,
    COMPARISON_PROBLEM_LISTING_TOO_LARGE,
    COMPARISON_PROBLEM_MISSING_OBJECT,
    COMPARISON_PROBLEM_MULTIPLE_REPOSITORIES,
    COMPARISON_PROBLEM_NO_BRANCH_POINT,
    COMPARISON_PROBLEM_NO_COMMITS,
    COMPARISON_PROBLEM_NO_DEFAULT_BRANCH,
    COMPARISON_PROBLEM_NOT_A_REPOSITORY,
    COMPARISON_PROBLEM_PARTIAL_CLONE_NEEDS_NEWER_GIT,
    COMPARISON_PROBLEM_TIMEOUT,
    COMPARISON_PROBLEM_TOO_MANY_FILES,
    CONTENT_ENCODING_BINARY,
    CONTENT_ENCODING_UTF8,
    GIT_ENTRY_KIND_FILE,
    GIT_ENTRY_KIND_SUBMODULE,
    GIT_ENTRY_KIND_SYMLINK,
    GIT_ENTRY_KIND_UNSPECIFIED,
    ChangedFile,
    CommittedFile,
    Comparison,
    ComparisonField,
    ComparisonKind,
    ComparisonProblem,
    ComparisonUnavailable,
    FileMetadata,
    GitEntryKind,
    ListWorkingCopyChangesRequest,
    ListWorkingCopyChangesResponse,
    ReadBaseFileRequest,
    ReadBaseFileResponse,
    ResolveWorkingCopyBaseRequest,
    ResolveWorkingCopyBaseResponse,
    WorkingCopyBase,
    WorkingFileMetadata,
)
from omnigent.entities.environment_filesystem import InvalidPath
from omnigent.host.workspace_git.files import is_inside
from omnigent.host.workspace_git.runner import (
    LISTING_OUTPUT_BYTES,
    Deadline,
    GitError,
    GitProblem,
    GitRepository,
    failure,
    is_object_name,
)
from omnigent.inner.async_utils import run_sync_on_thread
from omnigent.runner.environment_filesystem import _lstat_beneath, _validate_path
from omnigent.workspace_fs import guess_content_type

LISTING_DEADLINE_S = 20.0
READ_DEADLINE_S = 5.0
# Changed files a listing may return.
MAXIMUM_CHANGED_FILES = 10_000
# Blob bytes a ReadBaseFile sends when the request names no limit.
DEFAULT_MAXIMUM_READ_BYTES = 10 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
# Paths per `numstat` call, by total length.
_ARGUMENT_BATCH_BYTES = 64 * 1024
_MODE_SYMLINK = 0o120000
_MODE_SUBMODULE = 0o160000
_STATUS_BY_LETTER = {
    "A": CHANGE_STATUS_ADDED,
    "D": CHANGE_STATUS_DELETED,
    "T": CHANGE_STATUS_TYPE_CHANGED,
    "U": CHANGE_STATUS_CONFLICTED,
}
# The diff's submodule check reads each gitlink's commit without starting Git in the submodule.
_DIFF_OPTIONS = ("--ignore-submodules=dirty",)


class ComparisonUnavailableError(Exception):
    """A comparison that can't be made, carrying the ComparisonUnavailable message's fields.

    :param problem: A ``COMPARISON_PROBLEM_*`` value.
    :param field: Which requested revision the problem is about, if any.
    :param reference: For NO_BRANCH_POINT, the ref compared with.
    :param shallow: For NO_BRANCH_POINT, whether the clone is shallow.
    :param detail: For GIT_FAILED, Git's last error line.
    """

    def __init__(
        self,
        problem: ComparisonProblem,
        *,
        field: ComparisonField = COMPARISON_FIELD_UNSPECIFIED,
        reference: str = "",
        shallow: bool = False,
        detail: str = "",
    ) -> None:
        super().__init__(detail or str(problem))
        self.problem = problem
        self.field = field
        self.reference = reference
        self.shallow = shallow
        self.detail = detail

    def to_message(self) -> ComparisonUnavailable:
        return ComparisonUnavailable(
            problem=self.problem,
            field=self.field,
            reference=self.reference,
            shallow=self.shallow,
            detail=self.detail,
        )


_PROBLEM_BY_GIT_PROBLEM = {
    GitProblem.UNAVAILABLE: COMPARISON_PROBLEM_GIT_UNAVAILABLE,
    GitProblem.TIMEOUT: COMPARISON_PROBLEM_TIMEOUT,
    GitProblem.OUTPUT_TOO_LARGE: COMPARISON_PROBLEM_LISTING_TOO_LARGE,
    GitProblem.MISSING_OBJECT: COMPARISON_PROBLEM_MISSING_OBJECT,
    GitProblem.PARTIAL_CLONE_NEEDS_NEWER_GIT: COMPARISON_PROBLEM_PARTIAL_CLONE_NEEDS_NEWER_GIT,
    GitProblem.INVALID_REVISION: COMPARISON_PROBLEM_INVALID_REVISION,
    GitProblem.UNSAFE_CONFIGURATION: COMPARISON_PROBLEM_GIT_FAILED,
    GitProblem.FAILED: COMPARISON_PROBLEM_GIT_FAILED,
}


def as_unavailable(error: GitError | ComparisonUnavailableError) -> ComparisonUnavailableError:
    """The comparison problem an error stands for; a Git error maps by its kind."""
    if isinstance(error, ComparisonUnavailableError):
        return error
    problem = _PROBLEM_BY_GIT_PROBLEM[error.problem]
    detail = error.detail if problem == COMPARISON_PROBLEM_GIT_FAILED else ""
    return ComparisonUnavailableError(problem, detail=detail)


COMPARISON_ERRORS = (GitError, ComparisonUnavailableError)


def connect_error(error: GitError) -> ConnectError:
    """The RPC error for a Git error outside a comparison answer."""
    if error.problem is GitProblem.TIMEOUT:
        return ConnectError(Code.DEADLINE_EXCEEDED, "Git took too long")
    if error.problem is GitProblem.MISSING_OBJECT:
        return ConnectError(Code.NOT_FOUND, "This clone doesn't have that object")
    if error.problem is GitProblem.PARTIAL_CLONE_NEEDS_NEWER_GIT:
        return ConnectError(Code.FAILED_PRECONDITION, error.problem.value)
    return ConnectError(Code.UNAVAILABLE, error.detail or error.problem.value)


@dataclass(frozen=True)
class ComparisonBase:
    """What a comparison compares with, named by full object names.

    :param kind: A ``COMPARISON_KIND_*`` value.
    :param head: The head commit.
    :param base: The commit compared with: the branch point, or the merge base for revisions.
    :param reference: The ref compared with, or for revisions the base as asked for.
    :param reference_commit: The commit ``reference`` names.
    :param base_display: For revisions, the base revision as asked for or defaulted.
    :param head_display: For revisions, the head revision as asked for; empty for the working copy.
    :param head_is_working_copy: For revisions, whether the head is the working copy.
    """

    kind: ComparisonKind
    head: str
    base: str
    reference: str = ""
    reference_commit: str = ""
    base_display: str = ""
    head_display: str = ""
    head_is_working_copy: bool = False

    def to_message(self) -> WorkingCopyBase:
        message = WorkingCopyBase(
            kind=self.kind,
            head=self.head,
            base=self.base,
            reference=self.reference,
            reference_commit=self.reference_commit,
        )
        if self.kind == COMPARISON_KIND_REVISIONS:
            message.merge_base = self.base
            message.base_display = self.base_display
            message.head_display = self.head_display
            message.head_is_working_copy = self.head_is_working_copy
        return message


@dataclass
class WorkingCopyChanges:
    """Changed files beneath the workspace, sorted by path.

    :param unrepresentable_names: Changed files left out because their names aren't UTF-8.
    """

    base: ComparisonBase
    files: list[ChangedFile] = field(default_factory=list)
    unrepresentable_names: int = 0


def first_line(output: bytes) -> str:
    """The first line of Git's output, trimmed, or empty."""
    lines = output.decode("utf-8", "replace").splitlines()
    return lines[0].strip() if lines else ""


async def run_allowing_absent(
    repository: GitRepository, arguments: Sequence[str], deadline: Deadline
) -> bytes | None:
    """The output of a command whose exit status 1 without errors means "not there"; else None.

    Any other failure, including a signal, stays an error.
    """
    output = await repository.run_unchecked(arguments, deadline=deadline)
    if output.success:
        return output.stdout
    if output.returncode == 1 and not output.stderr.strip():
        return None
    raise failure(output, arguments[0])


async def verify_commit(
    repository: GitRepository, revision: str, deadline: Deadline
) -> str | None:
    """The full name of the commit ``revision`` names, or None when it names none here."""
    output = await run_allowing_absent(
        repository,
        ["rev-parse", "--verify", "-q", "--end-of-options", f"{revision}^{{commit}}"],
        deadline,
    )
    if output is None:
        return None
    commit = first_line(output)
    return commit if is_object_name(commit) else None


async def default_branch(repository: GitRepository, deadline: Deadline) -> tuple[str, str] | None:
    """The default branch as a full ref and its short name, or None when there is none.

    The remote is the current branch's upstream remote, else its push remote, else ``origin``.
    That remote's default branch wins, then an upstream naming a different branch, then
    ``origin/HEAD``.
    """
    branch_output = await run_allowing_absent(repository, ["symbolic-ref", "-q", "HEAD"], deadline)
    branch_ref = first_line(branch_output) if branch_output else ""
    remote: str | None = None
    upstream_candidate: str | None = None
    if branch_ref:
        tracking = await repository.run(
            [
                "for-each-ref",
                "--format=%(refname)%00%(upstream)%00%(upstream:remotename)%00%(push)"
                "%00%(push:remotename)",
                "--",
                branch_ref,
            ],
            deadline=deadline,
        )
        fields = tracking.decode("utf-8", "replace").rstrip("\n").split("\0")
        if len(fields) == 5:
            _ref, upstream, upstream_remote, _push, push_remote = fields
            remote = next(
                (name for name in (upstream_remote, push_remote) if name not in ("", ".")), None
            )
            upstream_prefix = f"refs/remotes/{upstream_remote}/"
            upstream_branch = (
                upstream[len(upstream_prefix) :] if upstream.startswith(upstream_prefix) else ""
            )
            branch_name = branch_ref.removeprefix("refs/heads/")
            if upstream_branch and upstream_branch != branch_name:
                upstream_candidate = upstream
    remote_name = remote or "origin"
    found = await _remote_default_ref(repository, remote_name, deadline)
    if found is not None:
        return found
    if upstream_candidate is not None and await verify_commit(
        repository, upstream_candidate, deadline
    ):
        return upstream_candidate, upstream_candidate.removeprefix("refs/remotes/")
    if remote_name == "origin":
        return None
    return await _remote_default_ref(repository, "origin", deadline)


async def _remote_default_ref(
    repository: GitRepository, remote: str, deadline: Deadline
) -> tuple[str, str] | None:
    output = await run_allowing_absent(
        repository, ["symbolic-ref", "-q", f"refs/remotes/{remote}/HEAD"], deadline
    )
    if output is None:
        return None
    target = first_line(output)
    if (
        not target.startswith("refs/remotes/")
        or await verify_commit(repository, target, deadline) is None
    ):
        return None
    return target, target.removeprefix("refs/remotes/")


async def resolve_base(
    repository: GitRepository, kind: ComparisonKind, deadline: Deadline
) -> ComparisonBase:
    """Resolve HEAD, or the branch point with the default branch, for a comparison kind.

    :raises ComparisonUnavailableError: When HEAD, the default branch or their merge base
        is absent.
    """
    head = await verify_commit(repository, "HEAD", deadline)
    if head is None:
        raise ComparisonUnavailableError(COMPARISON_PROBLEM_NO_COMMITS)
    if kind == COMPARISON_KIND_LAST_COMMIT:
        return ComparisonBase(kind=kind, head=head, base=head)
    found = await default_branch(repository, deadline)
    if found is None:
        raise ComparisonUnavailableError(COMPARISON_PROBLEM_NO_DEFAULT_BRANCH)
    full_ref, short_ref = found
    reference_commit = await verify_commit(repository, full_ref, deadline)
    if reference_commit is None:
        raise ComparisonUnavailableError(COMPARISON_PROBLEM_NO_DEFAULT_BRANCH)
    output = await run_allowing_absent(
        repository, ["merge-base", "--end-of-options", head, reference_commit], deadline
    )
    if output is None:
        shallow_output = await repository.run(
            ["rev-parse", "--is-shallow-repository"], deadline=deadline
        )
        raise ComparisonUnavailableError(
            COMPARISON_PROBLEM_NO_BRANCH_POINT,
            reference=short_ref,
            shallow=first_line(shallow_output) == "true",
        )
    base = first_line(output)
    if not is_object_name(base):
        raise ComparisonUnavailableError(
            COMPARISON_PROBLEM_GIT_FAILED,
            detail=f"git merge-base printed an unexpected result: {base}",
        )
    return ComparisonBase(
        kind=kind,
        head=head,
        base=base,
        reference=short_ref,
        reference_commit=reference_commit,
    )


async def resolve_comparison(
    root: Path, comparison: Comparison, deadline: Deadline
) -> tuple[GitRepository, ComparisonBase]:
    """The repository of ``root`` and the base a comparison request names.

    :raises ComparisonUnavailableError: NOT_A_REPOSITORY outside a repository, or any
        problem the base resolution reports.
    """
    repository = await GitRepository.discover(root, deadline)
    if repository is None:
        raise ComparisonUnavailableError(COMPARISON_PROBLEM_NOT_A_REPOSITORY)
    if comparison.kind == COMPARISON_KIND_REVISIONS:
        # Imported here: the revision comparison builds on this module.
        from omnigent.host.workspace_git.comparison import resolve_revisions

        base = await resolve_revisions(
            repository, comparison.base_revision, comparison.head_revision, deadline
        )
        return repository, base
    kind = (
        COMPARISON_KIND_LAST_COMMIT
        if comparison.kind == COMPARISON_KIND_UNSPECIFIED
        else comparison.kind
    )
    if kind not in (COMPARISON_KIND_LAST_COMMIT, COMPARISON_KIND_BRANCH_POINT):
        raise ComparisonUnavailableError(COMPARISON_PROBLEM_GIT_FAILED, detail="unknown kind")
    return repository, await resolve_base(repository, kind, deadline)


async def _run_all(*commands: Awaitable[bytes]) -> list[bytes]:
    """Run commands side by side and wait for all; the first failure is raised after they end."""
    results = await asyncio.gather(*commands, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return [result for result in results if isinstance(result, bytes)]


def _mode(text: str) -> int:
    try:
        return int(text, 8)
    except ValueError:
        return 0


def _entry_kind(mode: int) -> GitEntryKind:
    if mode == 0:
        return GIT_ENTRY_KIND_UNSPECIFIED
    if mode == _MODE_SYMLINK:
        return GIT_ENTRY_KIND_SYMLINK
    if mode == _MODE_SUBMODULE:
        return GIT_ENTRY_KIND_SUBMODULE
    return GIT_ENTRY_KIND_FILE


def _is_object_present(name: str) -> bool:
    return is_object_name(name) and any(character != "0" for character in name)


def parse_raw_diff(output: bytes, *, committed_head: bool) -> tuple[list[ChangedFile], int]:
    """Changed files from ``diff --raw -z``, and how many names weren't UTF-8.

    With ``committed_head``, each record's new object is the head's blob.
    :raises ComparisonUnavailableError: TOO_MANY_FILES past the listing's limit.
    """
    records = iter(output.split(b"\0"))
    entries: list[ChangedFile] = []
    unrepresentable = 0
    for header in records:
        if not header:
            continue
        path = next(records, None)
        if path is None:
            break
        parts = header.decode("utf-8", "replace").lstrip(":").split(" ")
        if len(parts) != 5:
            continue
        old_mode, new_mode, old_object, new_object, status_text = parts
        try:
            path_text = path.decode("utf-8")
        except UnicodeDecodeError:
            unrepresentable += 1
            continue
        if not is_inside(path_text):
            continue
        entry = ChangedFile(
            path=path_text,
            status=_STATUS_BY_LETTER.get(status_text[:1], CHANGE_STATUS_MODIFIED),
            base_kind=_entry_kind(_mode(old_mode)),
            working_kind=_entry_kind(_mode(new_mode)),
        )
        if _is_object_present(old_object):
            entry.base_object = old_object
        if committed_head and _is_object_present(new_object):
            entry.committed.CopyFrom(CommittedFile(object=new_object))
        entries.append(entry)
        if len(entries) > MAXIMUM_CHANGED_FILES:
            raise ComparisonUnavailableError(COMPARISON_PROBLEM_TOO_MANY_FILES)
    # An unmerged path appears once per stage; keep the first entry.
    unique: list[ChangedFile] = []
    for entry in entries:
        if not unique or unique[-1].path != entry.path:
            unique.append(entry)
    return unique, unrepresentable


async def object_sizes(
    repository: GitRepository, objects: Sequence[str], deadline: Deadline
) -> dict[str, int | None]:
    """Each object's size, or None where this clone lacks it."""
    if not objects:
        return {}
    output = await repository.run(
        ["cat-file", "--batch-check"],
        deadline=deadline,
        stdin="".join(f"{name}\n" for name in objects).encode(),
        stdout_bytes=LISTING_OUTPUT_BYTES,
    )
    sizes: dict[str, int | None] = {}
    for line in output.decode("utf-8", "replace").splitlines():
        parts = line.split(" ")
        if len(parts) == 2 and parts[1] == "missing":
            sizes[parts[0]] = None
        elif len(parts) == 3:
            sizes[parts[0]] = int(parts[2]) if parts[2].isdigit() else None
    return sizes


async def mark_base_objects(
    repository: GitRepository, entries: Sequence[ChangedFile], deadline: Deadline
) -> None:
    """Record each base blob's size, and flag the ones this clone doesn't have."""
    objects = [
        entry.base_object
        for entry in entries
        if entry.base_object and entry.base_kind != GIT_ENTRY_KIND_SUBMODULE
    ]
    sizes = await object_sizes(repository, objects, deadline)
    for entry in entries:
        if entry.base_object not in sizes:
            continue
        size = sizes[entry.base_object]
        if size is None:
            entry.problem = CHANGED_FILE_PROBLEM_MISSING_OBJECT
        else:
            entry.base_size = size


async def mark_committed_objects(
    repository: GitRepository, entries: Sequence[ChangedFile], deadline: Deadline
) -> None:
    """Record each committed head blob's size, and flag the ones this clone doesn't have."""
    objects = [
        entry.committed.object
        for entry in entries
        if entry.committed.object and entry.working_kind != GIT_ENTRY_KIND_SUBMODULE
    ]
    sizes = await object_sizes(repository, objects, deadline)
    for entry in entries:
        if not entry.committed.object or entry.committed.object not in sizes:
            continue
        size = sizes[entry.committed.object]
        if size is None:
            entry.problem = CHANGED_FILE_PROBLEM_MISSING_OBJECT
        else:
            entry.committed.size = size


async def mark_filtered(
    repository: GitRepository, entries: Sequence[ChangedFile], deadline: Deadline
) -> None:
    """Flag files with a ``filter`` attribute, such as LFS pointers.

    Such a file's stored form differs from its working form, so it is never
    diffed, whether or not this machine has the driver configured.
    """
    if not entries:
        return
    output = await repository.run(
        ["check-attr", "-z", "--stdin", "filter"],
        deadline=deadline,
        stdin=b"".join(entry.path.encode() + b"\0" for entry in entries),
        stdout_bytes=LISTING_OUTPUT_BYTES,
    )
    records = output.split(b"\0")
    filtered: set[str] = set()
    for index in range(0, len(records) - 2, 3):
        path, _attribute, value = records[index : index + 3]
        if value not in (b"unspecified", b"unset", b""):
            filtered.add(path.decode("utf-8", "replace"))
    for entry in entries:
        if entry.path in filtered:
            entry.problem = CHANGED_FILE_PROBLEM_FILTERED


def _argument_batches(paths: Sequence[str]) -> list[list[str]]:
    """Split paths into lists of at most ``_ARGUMENT_BATCH_BYTES`` each."""
    batches: list[list[str]] = []
    batch: list[str] = []
    size = 0
    for path in paths:
        length = len(path.encode()) + 1
        if batch and size + length > _ARGUMENT_BATCH_BYTES:
            batches.append(batch)
            batch, size = [], 0
        batch.append(path)
        size += length
    if batch:
        batches.append(batch)
    return batches


async def count_lines(
    repository: GitRepository,
    entries: Sequence[ChangedFile],
    deadline: Deadline,
    numstat_arguments: Callable[[list[str]], list[str]],
) -> None:
    """Record line counts for the diff's unfiltered files, additions included.

    A binary file has no counts and is flagged instead. Pass only entries the
    diff listed: numstat says nothing about untracked files.
    :param numstat_arguments: The ``numstat`` command for a batch of paths.
    """
    countable = [
        entry.path
        for entry in entries
        if entry.problem == CHANGED_FILE_PROBLEM_UNSPECIFIED
        and GIT_ENTRY_KIND_SUBMODULE not in (entry.base_kind, entry.working_kind)
    ]
    counts: dict[str, tuple[int, int] | None] = {}
    collected = 0
    for batch in _argument_batches(countable):
        output = await repository.run(
            numstat_arguments(batch),
            deadline=deadline,
            stdout_bytes=max(0, LISTING_OUTPUT_BYTES - collected),
        )
        collected += len(output)
        for record in output.decode("utf-8", "replace").split("\0"):
            parts = record.split("\t", 2)
            if len(parts) != 3:
                continue
            added, removed, path = parts
            counts[path] = (
                (int(added), int(removed)) if added.isdigit() and removed.isdigit() else None
            )
    for entry in entries:
        if entry.path not in counts:
            continue
        line_counts = counts[entry.path]
        if line_counts is None:
            entry.binary = True
        else:
            entry.lines_added, entry.lines_removed = line_counts


def working_metadata(working_directory: Path, path: str) -> WorkingFileMetadata | None:
    """A listed file's own lstat metadata, so a symlink's, not its target's.

    No directory on the way may be a symlink, so nothing outside the
    working directory is ever described.
    """
    metadata = _lstat_beneath(working_directory, path)
    if metadata is None:
        return None
    return WorkingFileMetadata(
        size=metadata.st_size,
        modified_unix_nanos=metadata.st_mtime_ns,
        changed_unix_nanos=metadata.st_ctime_ns,
        inode=metadata.st_ino,
        device=metadata.st_dev,
    )


def _record_working_metadata(working_directory: Path, entries: Sequence[ChangedFile]) -> None:
    for entry in entries:
        if entry.working_kind != GIT_ENTRY_KIND_UNSPECIFIED:
            metadata = working_metadata(working_directory, entry.path)
            if metadata is not None:
                entry.working.CopyFrom(metadata)


def _untracked_entry(working_directory: Path, path: str) -> ChangedFile:
    metadata = _lstat_beneath(working_directory, path)
    is_link = metadata is not None and stat.S_ISLNK(metadata.st_mode)
    return ChangedFile(
        path=path,
        status=CHANGE_STATUS_ADDED,
        working_kind=GIT_ENTRY_KIND_SYMLINK if is_link else GIT_ENTRY_KIND_FILE,
    )


async def list_changes(
    repository: GitRepository, base: ComparisonBase, deadline: Deadline
) -> WorkingCopyChanges:
    """The working copy's changes against ``base``, beneath the workspace.

    Tracked changes come from ``git diff``; untracked files from Git's ignore rules.

    :raises ComparisonUnavailableError: When there are too many files, a file is in a nested
        repository, or Git can't answer.
    """
    diff_output, untracked_output, unmerged_output = await _run_all(
        repository.run(
            [
                "diff",
                "--raw",
                "-z",
                "--no-abbrev",
                "--no-renames",
                *_DIFF_OPTIONS,
                "--relative",
                base.base,
                "--",
                ".",
            ],
            deadline=deadline,
            stdout_bytes=LISTING_OUTPUT_BYTES,
        ),
        repository.run(
            ["ls-files", "--others", "--exclude-standard", "-z", "--", "."],
            deadline=deadline,
            stdout_bytes=LISTING_OUTPUT_BYTES,
        ),
        repository.run(
            ["ls-files", "--unmerged", "-z", "--", "."],
            deadline=deadline,
            stdout_bytes=LISTING_OUTPUT_BYTES,
        ),
    )
    entries, unrepresentable = parse_raw_diff(diff_output, committed_head=False)
    listed_by_diff = list(entries)
    conflicted = _conflicted_paths(unmerged_output)
    for entry in entries:
        if entry.path in conflicted:
            entry.status = CHANGE_STATUS_CONFLICTED
    untracked: list[str] = []
    for raw_path in untracked_output.split(b"\0"):
        if not raw_path:
            continue
        if raw_path.endswith(b"/"):
            raise ComparisonUnavailableError(COMPARISON_PROBLEM_MULTIPLE_REPOSITORIES)
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            unrepresentable += 1
            continue
        if not is_inside(path):
            continue
        untracked.append(path)
        if len(entries) + len(untracked) > MAXIMUM_CHANGED_FILES:
            raise ComparisonUnavailableError(COMPARISON_PROBLEM_TOO_MANY_FILES)
    # Stat calls run on a thread, so a slow disk doesn't stall other requests.
    entries += await run_sync_on_thread(
        lambda: [_untracked_entry(repository.working_directory, path) for path in untracked]
    )
    await mark_base_objects(repository, entries, deadline)
    await mark_filtered(repository, entries, deadline)
    await count_lines(
        repository,
        listed_by_diff,
        deadline,
        lambda paths: [
            "diff",
            "--numstat",
            "-z",
            "--no-renames",
            *_DIFF_OPTIONS,
            "--relative",
            base.base,
            "--",
            *paths,
        ],
    )
    await run_sync_on_thread(_record_working_metadata, repository.working_directory, entries)
    entries.sort(key=lambda entry: entry.path)
    return WorkingCopyChanges(base=base, files=entries, unrepresentable_names=unrepresentable)


def _conflicted_paths(output: bytes) -> set[str]:
    """Paths from ``ls-files --unmerged -z``, whose records end in a tab and the path."""
    paths: set[str] = set()
    for record in output.split(b"\0"):
        _, separator, path = record.partition(b"\t")
        if separator:
            try:
                paths.add(path.decode("utf-8"))
            except UnicodeDecodeError:
                continue
    return paths


def _is_utf8(content: bytes) -> bool:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _validated_relative_path(path: str) -> str:
    try:
        relative = _validate_path(path)
    except InvalidPath as error:
        raise ConnectError(Code.INVALID_ARGUMENT, f"path: {error}") from error
    if not relative:
        raise ConnectError(Code.INVALID_ARGUMENT, "path: name a file")
    return relative


async def _blob_size_at(
    repository: GitRepository,
    request: ReadBaseFileRequest,
    relative: str,
    deadline: Deadline,
) -> int:
    """The size of the blob the request names, checked to be the path's entry at the commit."""
    top_path = repository.prefix + relative
    listing = await repository.run(
        ["ls-tree", "-z", "--full-tree", "--no-abbrev", request.commit, "--", top_path],
        deadline=deadline,
    )
    # `<mode> <type> <object>\t<path>`, NUL-terminated.
    record = listing.split(b"\0", 1)[0].decode("utf-8", "replace")
    metadata, _separator, listed_path = record.partition("\t")
    fields = metadata.split(" ")
    if (
        len(fields) != 3
        or fields[1] != "blob"
        or fields[2] != request.object
        or listed_path != top_path
    ):
        raise ConnectError(
            Code.FAILED_PRECONDITION, "That path at this commit isn't the blob requested"
        )
    size = (await object_sizes(repository, [request.object], deadline)).get(request.object)
    if size is None:
        raise ConnectError(Code.NOT_FOUND, "This clone doesn't have that file's content")
    return size


async def resolve_working_copy_base(
    root: Path, request: ResolveWorkingCopyBaseRequest
) -> ResolveWorkingCopyBaseResponse:
    """The base a working-copy comparison compares with, or why there is none."""
    deadline = Deadline(LISTING_DEADLINE_S)
    try:
        _repository, base = await resolve_comparison(root, request.comparison, deadline)
    except COMPARISON_ERRORS as error:
        return ResolveWorkingCopyBaseResponse(
            base=WorkingCopyBase(unavailable=as_unavailable(error).to_message())
        )
    return ResolveWorkingCopyBaseResponse(base=base.to_message())


async def list_working_copy_changes(
    root: Path, request: ListWorkingCopyChangesRequest
) -> ListWorkingCopyChangesResponse:
    """The changes against the requested base, beneath ``root``.

    A comparison that can't be made answers with its reason in ``base.unavailable``.
    """
    deadline = Deadline(LISTING_DEADLINE_S)
    try:
        repository, base = await resolve_comparison(root, request.comparison, deadline)
        if base.kind == COMPARISON_KIND_REVISIONS:
            # Imported here: the revision comparison builds on this module.
            from omnigent.host.workspace_git.comparison import list_revision_changes

            changes = await list_revision_changes(repository, base, deadline)
        else:
            changes = await list_changes(repository, base, deadline)
    except COMPARISON_ERRORS as error:
        return ListWorkingCopyChangesResponse(
            base=WorkingCopyBase(unavailable=as_unavailable(error).to_message())
        )
    return ListWorkingCopyChangesResponse(
        base=changes.base.to_message(),
        files=changes.files,
        unrepresentable_names=changes.unrepresentable_names,
    )


async def read_base_file(
    root: Path, request: ReadBaseFileRequest
) -> AsyncGenerator[ReadBaseFileResponse]:
    """Stream one file's content at a commit, when its entry there is the blob requested.

    The first message carries the metadata; a blob over ``maximum_bytes`` sends only that,
    with ``truncated`` set.

    :raises ConnectError: INVALID_ARGUMENT for a malformed name or path, FAILED_PRECONDITION
        when the path at the commit isn't that blob, NOT_FOUND when this clone lacks it.
    """
    if not (is_object_name(request.commit) and is_object_name(request.object)):
        raise ConnectError(Code.INVALID_ARGUMENT, "commit and object must be full object names")
    relative = _validated_relative_path(request.path)
    content_type = guess_content_type(relative) or ""
    deadline = Deadline(READ_DEADLINE_S)
    try:
        repository = await GitRepository.discover(root, deadline)
        if repository is None:
            raise ConnectError(Code.FAILED_PRECONDITION, "The workspace isn't in a Git repository")
        size = await _blob_size_at(repository, request, relative, deadline)
        # Larger requests are lowered to the server's limit, as for ReadFile.
        limit = min(
            request.maximum_bytes or DEFAULT_MAXIMUM_READ_BYTES, DEFAULT_MAXIMUM_READ_BYTES
        )
        if size > limit:
            yield ReadBaseFileResponse(
                metadata=FileMetadata(size=0, truncated=True, content_type=content_type)
            )
            return
        content = await repository.run(
            ["cat-file", "blob", request.object], deadline=deadline, stdout_bytes=size
        )
    except GitError as error:
        raise connect_error(error) from error
    yield ReadBaseFileResponse(
        metadata=FileMetadata(
            size=len(content),
            truncated=False,
            encoding=CONTENT_ENCODING_UTF8 if _is_utf8(content) else CONTENT_ENCODING_BINARY,
            content_type=content_type,
        )
    )
    for start in range(0, len(content), _READ_CHUNK_BYTES):
        yield ReadBaseFileResponse(data=content[start : start + _READ_CHUNK_BYTES])
