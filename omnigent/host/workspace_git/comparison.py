"""Comparing revisions a person typed: the merge base of a base and a head, compared with the head.

The head is the working copy or a commit. Every revision is checked before Git sees it, and Git
only reads: names are resolved with ``rev-parse --verify --end-of-options``, and listings use the
same commands, limits and safeguards as the working-copy comparison.
"""

from __future__ import annotations

from pathlib import Path

from connectrpc.code import Code
from connectrpc.errors import ConnectError

from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    COMPARISON_FIELD_BASE,
    COMPARISON_FIELD_BOTH,
    COMPARISON_FIELD_HEAD,
    COMPARISON_KIND_REVISIONS,
    COMPARISON_PROBLEM_BASE_NOT_FOUND,
    COMPARISON_PROBLEM_GIT_FAILED,
    COMPARISON_PROBLEM_HEAD_NOT_FOUND,
    COMPARISON_PROBLEM_INVALID_REVISION,
    COMPARISON_PROBLEM_NO_COMMITS,
    COMPARISON_PROBLEM_NO_COMMON_HISTORY,
    COMPARISON_PROBLEM_NOT_A_REPOSITORY,
    BranchCommit,
    ComparisonField,
    ListBranchCommitsRequest,
    ListBranchCommitsResponse,
    ListRepositoryRefsRequest,
    ListRepositoryRefsResponse,
    WorkingCopyBase,
)
from omnigent.host.workspace_git.runner import (
    LISTING_OUTPUT_BYTES,
    Deadline,
    GitError,
    GitRepository,
    check_revision,
    is_object_name,
)
from omnigent.host.workspace_git.working_copy import (
    COMPARISON_ERRORS,
    LISTING_DEADLINE_S,
    ComparisonBase,
    ComparisonUnavailableError,
    WorkingCopyChanges,
    as_unavailable,
    connect_error,
    count_lines,
    default_branch,
    first_line,
    list_changes,
    mark_base_objects,
    mark_committed_objects,
    mark_filtered,
    parse_raw_diff,
    run_allowing_absent,
    verify_commit,
)

# Git's empty tree, in SHA-1 and SHA-256 repositories. As a base it shows a root commit's whole
# tree as added.
EMPTY_TREES = (
    "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",
)
MAXIMUM_REFS = 1_000
MAXIMUM_BRANCH_COMMITS = 200
# Remote HEADs a ref listing may skip while still filling its limit; one per remote.
_REMOTE_HEAD_ALLOWANCE = 64
_LOG_FORMAT = "--format=%H%x1f%P%x1f%at%x1f%an%x1f%s"


def _checked_revision(text: str, field: ComparisonField) -> str | None:
    """The trimmed revision, or None when it is empty.

    :raises ComparisonUnavailableError: INVALID_REVISION for a control character or anything
        :func:`check_revision` refuses, so Git never sees it.
    """
    revision = text.strip()
    if not revision:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in revision):
        raise ComparisonUnavailableError(COMPARISON_PROBLEM_INVALID_REVISION, field=field)
    try:
        check_revision(revision)
    except GitError:
        raise ComparisonUnavailableError(
            COMPARISON_PROBLEM_INVALID_REVISION, field=field
        ) from None
    return revision


async def resolve_revisions(
    repository: GitRepository, base_revision: str, head_revision: str, deadline: Deadline
) -> ComparisonBase:
    """Resolve a base and head as typed, and their merge base.

    An empty base means the default branch, else HEAD; an empty head means the working copy.

    :raises ComparisonUnavailableError: for an invalid, unknown or unrelated revision.
    """
    base_text = _checked_revision(base_revision, COMPARISON_FIELD_BASE)
    head_text = _checked_revision(head_revision, COMPARISON_FIELD_HEAD)
    if base_text in EMPTY_TREES and head_text is not None:
        return await _against_empty_tree(repository, base_text, head_text, deadline)
    if base_text is None:
        found = await default_branch(repository, deadline)
        base_resolved, base_display = found if found is not None else ("HEAD", "HEAD")
    else:
        base_resolved = base_display = base_text
    if head_text is None:
        head_commit = await verify_commit(repository, "HEAD", deadline)
        if head_commit is None:
            raise ComparisonUnavailableError(COMPARISON_PROBLEM_NO_COMMITS)
    else:
        head_commit = await verify_commit(repository, head_text, deadline)
    base_commit = await verify_commit(repository, base_resolved, deadline)
    if base_commit is None and head_commit is None:
        raise ComparisonUnavailableError(
            COMPARISON_PROBLEM_BASE_NOT_FOUND, field=COMPARISON_FIELD_BOTH
        )
    if base_commit is None:
        raise ComparisonUnavailableError(
            COMPARISON_PROBLEM_BASE_NOT_FOUND, field=COMPARISON_FIELD_BASE
        )
    if head_commit is None:
        raise ComparisonUnavailableError(
            COMPARISON_PROBLEM_HEAD_NOT_FOUND, field=COMPARISON_FIELD_HEAD
        )
    output = await run_allowing_absent(
        repository, ["merge-base", "--end-of-options", base_commit, head_commit], deadline
    )
    if output is None:
        raise ComparisonUnavailableError(
            COMPARISON_PROBLEM_NO_COMMON_HISTORY, field=COMPARISON_FIELD_BOTH
        )
    merge_base = first_line(output)
    if not is_object_name(merge_base):
        raise ComparisonUnavailableError(
            COMPARISON_PROBLEM_GIT_FAILED,
            detail=f"git merge-base printed an unexpected result: {merge_base}",
        )
    return ComparisonBase(
        kind=COMPARISON_KIND_REVISIONS,
        head=head_commit,
        base=merge_base,
        reference=base_display,
        reference_commit=base_commit,
        base_display=base_display,
        head_display=head_text or "",
        head_is_working_copy=head_text is None,
    )


async def _against_empty_tree(
    repository: GitRepository, empty_tree: str, head_text: str, deadline: Deadline
) -> ComparisonBase:
    """A root commit compared with the empty tree, so everything in it is added."""
    head_commit = await verify_commit(repository, head_text, deadline)
    if head_commit is None:
        raise ComparisonUnavailableError(
            COMPARISON_PROBLEM_HEAD_NOT_FOUND, field=COMPARISON_FIELD_HEAD
        )
    # The other hash format's empty tree can't be compared with this repository's commits.
    if len(head_commit) != len(empty_tree):
        raise ComparisonUnavailableError(
            COMPARISON_PROBLEM_BASE_NOT_FOUND, field=COMPARISON_FIELD_BASE
        )
    # Any other head would show its whole tree as added.
    if not await _is_root_commit(repository, head_commit, deadline):
        raise ComparisonUnavailableError(
            COMPARISON_PROBLEM_BASE_NOT_FOUND, field=COMPARISON_FIELD_BASE
        )
    return ComparisonBase(
        kind=COMPARISON_KIND_REVISIONS,
        head=head_commit,
        base=empty_tree,
        reference=empty_tree,
        reference_commit=empty_tree,
        base_display=empty_tree,
        head_display=head_text,
        head_is_working_copy=False,
    )


async def _is_root_commit(repository: GitRepository, commit: str, deadline: Deadline) -> bool:
    """Whether ``commit`` has no parent, read from its raw object.

    A shallow clone's boundary commit names its parent in the object, though the clone hides it.
    """
    object_text = (
        await repository.run(
            ["cat-file", "commit", commit], deadline=deadline, stdout_bytes=LISTING_OUTPUT_BYTES
        )
    ).decode("utf-8", "replace")
    for line in object_text.splitlines():
        if not line:
            break
        if line.startswith("parent "):
            return False
    return True


async def list_revision_changes(
    repository: GitRepository, base: ComparisonBase, deadline: Deadline
) -> WorkingCopyChanges:
    """The files that differ from the merge base.

    For a working-copy head, exactly as the working-copy comparison lists them; for a committed
    head, the tracked files that differ between the two commits, with each file's blob there.
    """
    if base.head_is_working_copy:
        return await list_changes(repository, base, deadline)
    output = await repository.run(
        [
            "diff",
            "--raw",
            "-z",
            "--no-abbrev",
            "--no-renames",
            "--ignore-submodules=dirty",
            "--relative",
            "--end-of-options",
            base.base,
            base.head,
            "--",
            ".",
        ],
        deadline=deadline,
        stdout_bytes=LISTING_OUTPUT_BYTES,
    )
    entries, unrepresentable = parse_raw_diff(output, committed_head=True)
    await mark_base_objects(repository, entries, deadline)
    await mark_committed_objects(repository, entries, deadline)
    await mark_filtered(repository, entries, deadline)
    await count_lines(
        repository,
        entries,
        deadline,
        lambda paths: [
            "diff",
            "--numstat",
            "-z",
            "--no-renames",
            "--ignore-submodules=dirty",
            "--relative",
            "--end-of-options",
            base.base,
            base.head,
            "--",
            *paths,
        ],
    )
    entries.sort(key=lambda entry: entry.path)
    return WorkingCopyChanges(base=base, files=entries, unrepresentable_names=unrepresentable)


def _is_remote_head(full_name: str, symbolic_target: str) -> bool:
    """Whether a ref is a remote's HEAD: a symbolic ref directly under a remote.

    A branch or tag merely ending in ``/HEAD`` is an ordinary ref.
    """
    if not full_name.startswith("refs/remotes/") or not symbolic_target:
        return False
    remainder = full_name.removeprefix("refs/remotes/")
    return remainder.partition("/")[2] == "HEAD"


def _usable_refs(output: str, requested: int) -> tuple[list[str], bool]:
    """The short names from a listing that asked Git for ``requested`` refs, and if more exist."""
    listed: list[tuple[str, str, str]] = []
    for line in output.splitlines():
        fields = line.split("\0")
        if len(fields) < 2:
            continue
        listed.append((fields[0], fields[1], fields[2] if len(fields) > 2 else ""))
    # Git stopped at the count asked for, so there may be more than were listed.
    limited = len(listed) >= requested
    names = [
        short for full, short, symbolic in listed if short and not _is_remote_head(full, symbolic)
    ]
    truncated = len(names) > MAXIMUM_REFS or limited
    return names[:MAXIMUM_REFS], truncated


async def list_repository_refs(
    root: Path, _request: ListRepositoryRefsRequest
) -> ListRepositoryRefsResponse:
    """Branch and tag short names, local branches first, and the default branch's name.

    :raises ConnectError: FAILED_PRECONDITION outside a repository.
    """
    deadline = Deadline(LISTING_DEADLINE_S)
    try:
        repository = await GitRepository.discover(root, deadline)
        if repository is None:
            raise ConnectError(Code.FAILED_PRECONDITION, "The workspace isn't in a Git repository")
        requested = MAXIMUM_REFS + 1 + _REMOTE_HEAD_ALLOWANCE
        output = await repository.run(
            [
                "for-each-ref",
                f"--count={requested}",
                "--format=%(refname)%00%(refname:short)%00%(symref)",
                "--",
                "refs/heads",
                "refs/remotes",
                "refs/tags",
            ],
            deadline=deadline,
            stdout_bytes=LISTING_OUTPUT_BYTES,
        )
        names, truncated = _usable_refs(output.decode("utf-8", "replace"), requested)
        default = await default_branch(repository, deadline)
    except GitError as error:
        raise connect_error(error) from error
    return ListRepositoryRefsResponse(
        refs=names,
        truncated=truncated,
        default_branch_ref=default[1] if default is not None else "",
    )


def _parse_commits(output: bytes) -> list[BranchCommit]:
    """Commits from ``log -z`` records in :data:`_LOG_FORMAT`; malformed records are skipped."""
    commits: list[BranchCommit] = []
    for record in output.decode("utf-8", "replace").split("\0"):
        fields = record.lstrip("\n").split("\x1f", 4)
        if len(fields) < 4 or not is_object_name(fields[0]):
            continue
        object_name, parents, time_text, author_name = fields[:4]
        subject = fields[4] if len(fields) > 4 else ""
        commits.append(
            BranchCommit(
                object=object_name,
                subject=subject,
                author_name=author_name,
                author_time_unix=int(time_text) if time_text.isdigit() else 0,
                root=not parents.strip(),
            )
        )
    return commits


async def list_branch_commits(
    root: Path, request: ListBranchCommitsRequest
) -> ListBranchCommitsResponse:
    """The commits HEAD has beyond its merge base with the base, newest first.

    A base that can't be compared answers with its reason in ``base.unavailable``.
    """
    deadline = Deadline(LISTING_DEADLINE_S)
    try:
        repository = await GitRepository.discover(root, deadline)
        if repository is None:
            raise ComparisonUnavailableError(COMPARISON_PROBLEM_NOT_A_REPOSITORY)
        comparison = await resolve_revisions(repository, request.base_revision, "HEAD", deadline)
        output = await repository.run(
            [
                "log",
                "-z",
                f"--max-count={MAXIMUM_BRANCH_COMMITS + 1}",
                _LOG_FORMAT,
                "--end-of-options",
                comparison.head,
                f"^{comparison.base}",
                "--",
            ],
            deadline=deadline,
            stdout_bytes=LISTING_OUTPUT_BYTES,
        )
        commits = _parse_commits(output)
        truncated = len(commits) > MAXIMUM_BRANCH_COMMITS
        commits = commits[:MAXIMUM_BRANCH_COMMITS]
        # A root commit is listed as such only when its object really has no parent.
        for commit in commits:
            if commit.root:
                commit.root = await _is_root_commit(repository, commit.object, deadline)
    except COMPARISON_ERRORS as error:
        return ListBranchCommitsResponse(
            base=WorkingCopyBase(unavailable=as_unavailable(error).to_message())
        )
    return ListBranchCommitsResponse(
        base=comparison.to_message(), commits=commits, truncated=truncated
    )
