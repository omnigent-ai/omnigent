"""The working copy's changes and base reads, against real temporary repositories."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
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
    COMPARISON_KIND_BRANCH_POINT,
    COMPARISON_KIND_LAST_COMMIT,
    COMPARISON_PROBLEM_LISTING_TOO_LARGE,
    COMPARISON_PROBLEM_MULTIPLE_REPOSITORIES,
    COMPARISON_PROBLEM_NO_BRANCH_POINT,
    COMPARISON_PROBLEM_NO_COMMITS,
    COMPARISON_PROBLEM_NO_DEFAULT_BRANCH,
    COMPARISON_PROBLEM_NOT_A_REPOSITORY,
    COMPARISON_PROBLEM_PARTIAL_CLONE_NEEDS_NEWER_GIT,
    COMPARISON_PROBLEM_TOO_MANY_FILES,
    GIT_ENTRY_KIND_FILE,
    GIT_ENTRY_KIND_SUBMODULE,
    GIT_ENTRY_KIND_SYMLINK,
    ChangedFile,
    Comparison,
    ComparisonKind,
    FileMetadata,
    ListWorkingCopyChangesRequest,
    ListWorkingCopyChangesResponse,
    ReadBaseFileRequest,
    ReadBaseFileResponse,
    ResolveWorkingCopyBaseRequest,
    ResolveWorkingCopyBaseResponse,
)
from omnigent.host.workspace_git import runner, working_copy
from omnigent.host.workspace_git.working_copy import (
    list_working_copy_changes,
    read_base_file,
    resolve_working_copy_base,
)
from tests.host.test_workspace_git_runner import _git, _repository

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the runner signals POSIX process groups"
)

_FULL_NAME_LENGTH = 40


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Dev",
        "-c",
        "user.email=dev@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        message,
    )


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").strip()


def _add_remote(root: Path, name: str) -> None:
    _git(root, "config", f"remote.{name}.url", f"https://example.invalid/{name}.git")
    _git(root, "config", f"remote.{name}.fetch", f"+refs/heads/*:refs/remotes/{name}/*")


def _branch_fixture(tmp_path: Path) -> tuple[Path, str]:
    """main at c1 tracked by remote `databricks`; `feature` two commits ahead of c1.

    ``origin/HEAD`` names a fork's branch that is further behind.
    """
    root = _repository(tmp_path / "repo")
    _write(root / "edited.txt", "one\n")
    _commit(root, "c0")
    fork_commit = _head(root)
    _write(root / "base.txt", "base\n")
    _commit(root, "c1")
    branch_point = _head(root)
    _add_remote(root, "databricks")
    _add_remote(root, "origin")
    _git(root, "update-ref", "refs/remotes/databricks/master", branch_point)
    _git(root, "update-ref", "refs/remotes/origin/master", fork_commit)
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
    _git(root, "checkout", "-q", "-b", "feature")
    _git(root, "config", "branch.feature.remote", "databricks")
    _git(root, "config", "branch.feature.merge", "refs/heads/master")
    _write(root / "committed.txt", "on the branch\n")
    _commit(root, "c2")
    _write(root / "edited.txt", "one\ntwo\n")
    _commit(root, "c3")
    return root, branch_point


async def _changes(
    root: Path, kind: ComparisonKind = COMPARISON_KIND_LAST_COMMIT
) -> ListWorkingCopyChangesResponse:
    return await list_working_copy_changes(
        root, ListWorkingCopyChangesRequest(comparison=Comparison(kind=kind))
    )


async def _resolution(root: Path, kind: ComparisonKind) -> ResolveWorkingCopyBaseResponse:
    return await resolve_working_copy_base(
        root, ResolveWorkingCopyBaseRequest(comparison=Comparison(kind=kind))
    )


def _paths(response: ListWorkingCopyChangesResponse) -> list[str]:
    return [changed.path for changed in response.files]


def _file(response: ListWorkingCopyChangesResponse, path: str) -> ChangedFile:
    matches = [changed for changed in response.files if changed.path == path]
    assert matches, f"{path} not listed in {_paths(response)}"
    return matches[0]


def _unavailable_problem(response: ListWorkingCopyChangesResponse) -> int | None:
    if not response.base.HasField("unavailable"):
        return None
    return response.base.unavailable.problem


async def _read(root: Path, request: ReadBaseFileRequest) -> tuple[FileMetadata, bytes]:
    responses: list[ReadBaseFileResponse] = [
        response async for response in read_base_file(root, request)
    ]
    return responses[0].metadata, b"".join(response.data for response in responses)


def _snapshot(git_directory: Path) -> dict[Path, tuple[int, int, bytes]]:
    """Every file under ``.git`` with its size, modification time and bytes."""
    snapshot: dict[Path, tuple[int, int, bytes]] = {}
    for directory, _subdirectories, names in os.walk(git_directory):
        for name in names:
            path = Path(directory) / name
            metadata = path.lstat()
            snapshot[path] = (metadata.st_size, metadata.st_mtime_ns, path.read_bytes())
    return snapshot


def _child_directories(trace: Path) -> list[str]:
    """The directories, relative to their parent's, of the Git children a trace recorded."""
    events = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
    assert any(event["event"] == "start" for event in events), "tracing never started"
    return [event["cd"] for event in events if event["event"] == "child_start" and "cd" in event]


async def test_last_commit_lists_modified_deleted_and_untracked_files(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    _write(root / "gone.txt", "bye\n")
    _commit(root, "c1")
    _write(root / "a.txt", "a\nb\nc\n")
    (root / "gone.txt").unlink()
    _write(root / "new.txt", "hello\n")
    (root / "staged.bin").write_bytes(b"\0\1\2 not text")
    _write(root / "staged.txt", "one\ntwo\n")
    _git(root, "add", "staged.bin", "staged.txt")

    response = await _changes(root)
    assert _unavailable_problem(response) is None
    assert _paths(response) == ["a.txt", "gone.txt", "new.txt", "staged.bin", "staged.txt"]
    assert response.base.base == _head(root)
    assert len(response.base.base) == _FULL_NAME_LENGTH

    modified = _file(response, "a.txt")
    assert modified.status == CHANGE_STATUS_MODIFIED
    assert (modified.lines_added, modified.lines_removed) == (2, 0)
    assert len(modified.base_object) == _FULL_NAME_LENGTH
    assert modified.base_size == 2
    assert _file(response, "gone.txt").status == CHANGE_STATUS_DELETED
    added = _file(response, "new.txt")
    assert added.status == CHANGE_STATUS_ADDED
    assert added.base_object == ""
    # Untracked files are outside the diff, so they carry no counts.
    assert not added.HasField("lines_added") and not added.binary
    staged_binary = _file(response, "staged.bin")
    assert staged_binary.status == CHANGE_STATUS_ADDED
    assert staged_binary.binary and not staged_binary.HasField("lines_added")
    staged_text = _file(response, "staged.txt")
    assert (staged_text.lines_added, staged_text.lines_removed) == (2, 0)
    assert not staged_text.binary


async def test_a_slow_filesystem_never_stalls_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each listed file is stat'ed; on a slow disk that must not block other requests."""
    root = _repository(tmp_path / "repo")
    for index in range(4):
        _write(root / f"tracked{index}.txt", "x\n")
    _commit(root, "c1")
    for index in range(4):
        _write(root / f"tracked{index}.txt", "y\n")
        _write(root / f"untracked{index}.txt", "new\n")
    real_lstat_beneath = working_copy._lstat_beneath

    def slow_lstat_beneath(directory: Path, path: str) -> os.stat_result | None:
        time.sleep(0.1)
        return real_lstat_beneath(directory, path)

    monkeypatch.setattr(working_copy, "_lstat_beneath", slow_lstat_beneath)
    longest_stall = 0.0

    async def tick() -> None:
        nonlocal longest_stall
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(0.01)
            longest_stall = max(longest_stall, loop.time() - before)

    ticker = asyncio.ensure_future(tick())
    try:
        response = await _changes(root)
    finally:
        ticker.cancel()

    assert len(response.files) == 8 and response.files[0].HasField("working")
    assert longest_stall < 0.3


async def test_branch_point_uses_the_tracked_remote_and_includes_committed_files(
    tmp_path: Path,
) -> None:
    root, branch_point = _branch_fixture(tmp_path)
    _write(root / "untracked.txt", "new\n")

    response = await _changes(root, COMPARISON_KIND_BRANCH_POINT)
    assert _unavailable_problem(response) is None
    # The tracked remote's branch, not the fork that origin/HEAD names.
    assert response.base.reference == "databricks/master"
    assert response.base.base == branch_point
    assert _paths(response) == ["committed.txt", "edited.txt", "untracked.txt"]

    last = await _changes(root)
    assert _paths(last) == ["untracked.txt"]


async def test_branch_point_falls_back_to_origins_default_branch(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    first = _head(root)
    _add_remote(root, "origin")
    _git(root, "update-ref", "refs/remotes/origin/main", first)
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(root, "checkout", "-q", "-b", "topic")
    _write(root / "b.txt", "b\n")
    _commit(root, "c2")
    _git(root, "update-ref", "refs/remotes/origin/topic", _head(root))
    _git(root, "config", "branch.topic.remote", "origin")
    _git(root, "config", "branch.topic.merge", "refs/heads/topic")

    response = await _changes(root, COMPARISON_KIND_BRANCH_POINT)
    assert _unavailable_problem(response) is None
    assert response.base.reference == "origin/main"
    assert response.base.base == first
    assert _paths(response) == ["b.txt"]


async def test_branch_point_prefers_origins_default_over_another_origin_branch(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    main = _head(root)
    _write(root / "b.txt", "b\n")
    _commit(root, "c2")
    release = _head(root)
    _add_remote(root, "origin")
    _git(root, "update-ref", "refs/remotes/origin/main", main)
    _git(root, "update-ref", "refs/remotes/origin/release", release)
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(root, "checkout", "-q", "-b", "feature")
    _git(root, "config", "branch.feature.remote", "origin")
    _git(root, "config", "branch.feature.merge", "refs/heads/release")
    _write(root / "c.txt", "c\n")
    _commit(root, "c3")

    response = await _changes(root, COMPARISON_KIND_BRANCH_POINT)
    assert _unavailable_problem(response) is None
    assert response.base.reference == "origin/main"
    assert response.base.base == main


async def test_branch_point_reports_why_it_is_unavailable(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q", "-b", "main")
    assert _unavailable_problem(await _changes(empty, COMPARISON_KIND_BRANCH_POINT)) == (
        COMPARISON_PROBLEM_NO_COMMITS
    )

    lonely = _repository(tmp_path / "lonely")
    assert _unavailable_problem(await _changes(lonely, COMPARISON_KIND_BRANCH_POINT)) == (
        COMPARISON_PROBLEM_NO_DEFAULT_BRANCH
    )
    # HEAD still works without a default branch.
    assert _unavailable_problem(await _changes(lonely)) is None

    _git(lonely, "checkout", "-q", "--orphan", "other")
    _git(lonely, "rm", "-rq", "--cached", ".")
    _write(lonely / "b.txt", "b\n")
    _commit(lonely, "unrelated")
    _git(lonely, "update-ref", "refs/remotes/origin/main", _head(lonely))
    _git(lonely, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(lonely, "checkout", "-q", "main")
    response = await _changes(lonely, COMPARISON_KIND_BRANCH_POINT)
    assert _unavailable_problem(response) == COMPARISON_PROBLEM_NO_BRANCH_POINT
    assert response.base.unavailable.reference == "origin/main"
    assert not response.base.unavailable.shallow

    outside = tmp_path / "outside"
    outside.mkdir()
    assert _unavailable_problem(await _changes(outside)) == COMPARISON_PROBLEM_NOT_A_REPOSITORY


async def test_a_workspace_below_the_repository_top_sees_only_its_subtree(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    workspace = root / "w*[x]?"
    _write(workspace / "sub" / "f.txt", "a\n")
    _write(root / "other" / "g.txt", "b\n")
    _write(workspace / "top.txt", "c\n")
    _commit(root, "c1")
    _write(workspace / "sub" / "f.txt", "a\nb\n")
    _write(root / "other" / "g.txt", "changed\n")
    _write(workspace / "new.txt", "n\n")
    _write(root / "other" / "new.txt", "n\n")

    response = await _changes(workspace)
    assert _unavailable_problem(response) is None
    assert _paths(response) == ["new.txt", "sub/f.txt"]
    entry = _file(response, "sub/f.txt")
    assert entry.lines_added == 1

    def request(path: str, object_name: str) -> ReadBaseFileRequest:
        return ReadBaseFileRequest(
            commit=response.base.base, path=path, object=object_name, maximum_bytes=0
        )

    _metadata, content = await _read(workspace, request("sub/f.txt", entry.base_object))
    assert content == b"a\n"
    with pytest.raises(ConnectError) as escaped:
        await _read(workspace, request("../other/g.txt", entry.base_object))
    assert escaped.value.code == Code.INVALID_ARGUMENT
    with pytest.raises(ConnectError) as wrong_object:
        await _read(workspace, request("top.txt", entry.base_object))
    assert wrong_object.value.code == Code.FAILED_PRECONDITION


@pytest.mark.parametrize(
    ("maximum_bytes", "truncated"),
    [(1_000, True), (99_999, True), (100_000, False), (0, False), (10**9, True)],
    ids=["small", "one-short", "exact", "default-limit", "above-the-server-limit"],
)
async def test_base_reads_stop_at_the_allowance_without_partial_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maximum_bytes: int, truncated: bool
) -> None:
    # The server's own limit, lowered so a larger request needs no 10 MiB blob.
    if maximum_bytes > 100_000:
        monkeypatch.setattr(
            "omnigent.host.workspace_git.working_copy.DEFAULT_MAXIMUM_READ_BYTES", 50_000
        )
    root = _repository(tmp_path / "repo")
    _write(root / "big.txt", "x" * 100_000)
    _commit(root, "c1")
    _write(root / "big.txt", "small\n")
    response = await _changes(root)
    entry = _file(response, "big.txt")

    metadata, content = await _read(
        root,
        ReadBaseFileRequest(
            commit=response.base.base,
            path="big.txt",
            object=entry.base_object,
            maximum_bytes=maximum_bytes,
        ),
    )
    if truncated:
        assert metadata.truncated
        assert content == b""
    else:
        assert not metadata.truncated
        assert content == b"x" * 100_000


async def test_filtered_files_and_commands_never_run_configured_helpers(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    markers = tmp_path / "markers"
    markers.mkdir()
    clean_marker = markers / "clean"
    external_marker = markers / "external"
    textconv_marker = markers / "textconv"
    hook_marker = markers / "hook"
    _write(root / ".gitattributes", "*.dat filter=StrictCase diff=shown\n")
    _write(root / "data.dat", "one\n")
    _commit(root, "attributes")
    _git(root, "config", "filter.StrictCase.clean", f"touch {clean_marker}; cat")
    _git(root, "config", "filter.StrictCase.required", "true")
    _git(root, "config", "diff.shown.textconv", f"touch {textconv_marker}; cat")
    _git(root, "config", "diff.external", f"touch {external_marker}")
    _git(root, "config", "core.hooksPath", str(markers))
    (markers / "post-checkout").write_text(f"#!/bin/sh\ntouch {hook_marker}\n")
    (markers / "post-checkout").chmod(0o755)
    # Changed after the index was written, so Git must look at the content.
    _write(root / "data.dat", "one\ntwo\n")
    _write(root / "edited.txt", "edited\n")
    before = _snapshot(root / ".git")

    response = await _changes(root)
    assert _unavailable_problem(response) is None
    data = _file(response, "data.dat")
    assert data.problem == CHANGED_FILE_PROBLEM_FILTERED
    assert not data.HasField("lines_added")
    for marker in (clean_marker, external_marker, textconv_marker, hook_marker):
        assert not marker.exists(), f"{marker.name} ran"
    assert _snapshot(root / ".git") == before


async def test_symlinks_are_listed_by_their_own_kind(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    outside = tmp_path / "outside"
    _write(outside / "secret", "secret contents\n")
    _write(root / "regular.txt", "was a file\n")
    _write(root / "target.txt", "target\n")
    os.symlink("target.txt", root / "link")
    _commit(root, "c1")
    (root / "regular.txt").unlink()
    os.symlink("target.txt", root / "regular.txt")
    (root / "link").unlink()
    os.symlink("missing.txt", root / "link")
    os.symlink(outside / "secret", root / "escape")

    response = await _changes(root)
    changed = _file(response, "regular.txt")
    assert changed.status == CHANGE_STATUS_TYPE_CHANGED
    assert changed.base_kind == GIT_ENTRY_KIND_FILE
    assert changed.working_kind == GIT_ENTRY_KIND_SYMLINK
    assert _file(response, "link").working_kind == GIT_ENTRY_KIND_SYMLINK
    assert _file(response, "escape").working_kind == GIT_ENTRY_KIND_SYMLINK


async def test_a_conflicted_file_is_listed_once(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    _write(root / "c.txt", "base\n")
    _commit(root, "c1")
    _git(root, "checkout", "-q", "-b", "left")
    _write(root / "c.txt", "left\n")
    _commit(root, "left")
    _git(root, "checkout", "-q", "main")
    _write(root / "c.txt", "right\n")
    _commit(root, "right")
    merge = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e",
            "merge",
            "-q",
            "left",
        ],
        capture_output=True,
        check=False,
    )
    assert merge.returncode != 0, "the merge conflicts"

    response = await _changes(root)
    assert _unavailable_problem(response) is None
    conflicted = [changed for changed in response.files if changed.path == "c.txt"]
    assert len(conflicted) == 1
    assert conflicted[0].status == CHANGE_STATUS_CONFLICTED


async def test_untracked_files_follow_gits_ignore_rules_as_they_change(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    _write(root / ".gitignore", "*.log\n!keep.log\nbuild/\n")
    _write(root / "tracked.log", "tracked\n")
    _git(root, "add", "-f", "tracked.log")
    _commit(root, "c1")
    _write(root / ".git" / "info" / "exclude", "local.tmp\n")
    _write(root / "tracked.log", "tracked and edited\n")
    _write(root / "debug.log", "ignored\n")
    _write(root / "keep.log", "unignored\n")
    _write(root / "local.tmp", "excluded for this clone\n")
    _write(root / "build" / "out.txt", "ignored directory\n")
    _write(root / "new.txt", "new\n")
    os.symlink("build", root / "build-link")
    os.symlink("nowhere", root / "dangling")

    response = await _changes(root)
    assert _unavailable_problem(response) is None
    assert _paths(response) == ["build-link", "dangling", "keep.log", "new.txt", "tracked.log"]
    # An ignored file that is tracked still shows its edits.
    assert _file(response, "tracked.log").status == CHANGE_STATUS_MODIFIED
    assert _file(response, "build-link").working_kind == GIT_ENTRY_KIND_SYMLINK

    _write(root / ".gitignore", "*.log\nbuild/\n")
    _write(root / ".git" / "info" / "exclude", "local.tmp\nnew.txt\n")
    response = await _changes(root)
    assert _paths(response) == [".gitignore", "build-link", "dangling", "tracked.log"]
    assert _file(response, ".gitignore").status == CHANGE_STATUS_MODIFIED


async def test_a_nested_repository_makes_the_comparison_unavailable(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    nested = root / "vendor" / "nested"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q", "-b", "main")
    _write(nested / "file.txt", "nested\n")
    assert _unavailable_problem(await _changes(root)) == COMPARISON_PROBLEM_MULTIPLE_REPOSITORIES

    shutil.rmtree(root / "vendor")
    _git(root, "worktree", "add", "-q", "linked")
    assert _unavailable_problem(await _changes(root)) == COMPARISON_PROBLEM_MULTIPLE_REPOSITORIES


async def test_a_dirty_submodule_is_never_entered_and_its_filter_never_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submodule_origin = _repository(tmp_path / "submodule-origin")
    root = _repository(tmp_path / "superproject")
    _git(
        root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(submodule_origin),
        "sub",
    )
    _commit(root, "add submodule")
    submodule = root / "sub"
    # Everything is committed before the driver exists, so no setup step can run it.
    _write(submodule / ".gitattributes", "*.dat filter=marker\n")
    _write(submodule / "data.dat", "one\n")
    _commit(submodule, "data")
    _write(submodule / "a.txt", "advanced\n")
    _commit(submodule, "advance the submodule")
    marker = tmp_path / "marker"
    _git(submodule, "config", "filter.marker.clean", f"touch {marker}; cat")
    _git(submodule, "config", "remote.origin.promisor", "true")
    # The superproject's gitlink now differs from its recorded commit, and the submodule is dirty.
    _write(submodule / "data.dat", "one\ntwo\n")
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setitem(runner.test_environment, "GIT_TRACE2_EVENT", str(trace))

    response = await _changes(root)
    assert _unavailable_problem(response) is None
    assert _paths(response) == ["sub"]
    gitlink = _file(response, "sub")
    assert gitlink.status == CHANGE_STATUS_MODIFIED
    assert gitlink.base_kind == GIT_ENTRY_KIND_SUBMODULE
    assert gitlink.working_kind == GIT_ENTRY_KIND_SUBMODULE
    assert not marker.exists()
    assert "sub" not in _child_directories(trace)


async def test_listings_carry_working_metadata_that_changes_with_each_edit(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    _write(root / "tracked.txt", "one\n")
    _write(root / "gone.txt", "bye\n")
    _commit(root, "c1")
    _write(root / "tracked.txt", "two\n")
    _write(root / "untracked.txt", "aaaa\n")
    (root / "gone.txt").unlink()
    os.symlink("a-long-target-name", root / "link")

    before = await _changes(root)
    assert not any(
        changed.path == "gone.txt" and changed.HasField("working") for changed in before.files
    )
    assert _file(before, "untracked.txt").working.size == 5
    # A link's own size: the length of its target text.
    assert _file(before, "link").working.size == len("a-long-target-name")

    _write(root / "tracked.txt", "six\n")
    _write(root / "untracked.txt", "bbbb\n")
    after = await _changes(root)
    for path in ("tracked.txt", "untracked.txt"):
        old, new = _file(before, path), _file(after, path)
        assert (old.status, old.lines_added, old.lines_removed) == (
            new.status,
            new.lines_added,
            new.lines_removed,
        )
        assert old.working != new.working, path
    assert _file(before, "link").working == _file(after, "link").working


async def test_a_partial_clone_reports_missing_objects_instead_of_fetching(tmp_path: Path) -> None:
    origin = _repository(tmp_path / "origin")
    _git(origin, "config", "uploadpack.allowFilter", "true")
    _write(origin / "a.txt", "version one\n")
    _commit(origin, "c1")
    first = _head(origin)
    _write(origin / "a.txt", "version two\n")
    _commit(origin, "c2")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--filter=blob:none", f"file://{origin}", str(clone)],
        check=True,
        capture_output=True,
    )
    # The first commit's blob was never downloaded; compare with it.
    _add_remote(clone, "upstream")
    _git(clone, "update-ref", "refs/remotes/upstream/release", first)
    _git(clone, "config", "branch.main.remote", "upstream")
    _git(clone, "config", "branch.main.merge", "refs/heads/release")
    # Contacting the promisor remote would now fail or hang.
    shutil.move(str(origin), str(tmp_path / "origin-moved"))
    before = _snapshot(clone / ".git")

    response = await _changes(clone, COMPARISON_KIND_BRANCH_POINT)
    if _unavailable_problem(response) == COMPARISON_PROBLEM_PARTIAL_CLONE_NEEDS_NEWER_GIT:
        pytest.skip("this Git is too old to read partial clones without fetching")
    assert _unavailable_problem(response) is None
    assert response.base.base == first
    entry = _file(response, "a.txt")
    assert entry.problem == CHANGED_FILE_PROBLEM_MISSING_OBJECT
    assert not entry.HasField("lines_added")

    with pytest.raises(ConnectError) as missing:
        await _read(
            clone,
            ReadBaseFileRequest(
                commit=first, path="a.txt", object=entry.base_object, maximum_bytes=0
            ),
        )
    assert missing.value.code == Code.NOT_FOUND
    assert _snapshot(clone / ".git") == before


async def test_too_many_changed_files_and_oversized_listings_are_typed_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path / "repo")
    for name in ("one", "two", "three"):
        _write(root / f"{name}.txt", "x\n")
    monkeypatch.setattr(working_copy, "MAXIMUM_CHANGED_FILES", 2)
    assert _unavailable_problem(await _changes(root)) == COMPARISON_PROBLEM_TOO_MANY_FILES

    monkeypatch.setattr(working_copy, "MAXIMUM_CHANGED_FILES", 10_000)
    monkeypatch.setattr(working_copy, "LISTING_OUTPUT_BYTES", 10)
    assert _unavailable_problem(await _changes(root)) == COMPARISON_PROBLEM_LISTING_TOO_LARGE


async def test_names_that_are_not_utf8_are_counted_not_listed(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    try:
        (root / os.fsdecode(b"bad-\xff.txt")).write_text("x\n")
    except OSError:
        pytest.skip("this filesystem refuses names that aren't UTF-8")
    _write(root / "good.txt", "x\n")

    response = await _changes(root)
    assert _unavailable_problem(response) is None
    assert _paths(response) == ["good.txt"]
    assert response.unrepresentable_names == 1


async def test_a_base_read_refuses_a_malformed_name_and_a_missing_repository(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    object_name = _head(root)
    with pytest.raises(ConnectError) as short_commit:
        await _read(
            root,
            ReadBaseFileRequest(commit="abc", path="a.txt", object=object_name, maximum_bytes=0),
        )
    assert short_commit.value.code == Code.INVALID_ARGUMENT
    with pytest.raises(ConnectError) as escaping_path:
        await _read(
            root,
            ReadBaseFileRequest(
                commit=object_name, path="../a.txt", object=object_name, maximum_bytes=0
            ),
        )
    assert escaping_path.value.code == Code.INVALID_ARGUMENT
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ConnectError) as no_repository:
        await _read(
            outside,
            ReadBaseFileRequest(
                commit=object_name, path="a.txt", object=object_name, maximum_bytes=0
            ),
        )
    assert no_repository.value.code == Code.FAILED_PRECONDITION


async def test_resolving_a_branch_point_reports_the_base_or_its_problem(tmp_path: Path) -> None:
    root, branch_point = _branch_fixture(tmp_path)
    resolved = await _resolution(root, COMPARISON_KIND_BRANCH_POINT)
    assert not resolved.base.HasField("unavailable")
    assert resolved.base.base == branch_point
    assert resolved.base.reference == "databricks/master"

    outside = tmp_path / "outside"
    outside.mkdir()
    unavailable = await _resolution(outside, COMPARISON_KIND_BRANCH_POINT)
    assert unavailable.base.unavailable.problem == COMPARISON_PROBLEM_NOT_A_REPOSITORY


async def test_branch_point_prefers_the_upstream_remote_over_a_push_fork(tmp_path: Path) -> None:
    root, branch_point = _branch_fixture(tmp_path)
    # The branch pushes to a fork whose default branch is something else.
    _add_remote(root, "fork")
    _git(root, "update-ref", "refs/remotes/fork/main", _git(root, "rev-parse", "feature").strip())
    _git(root, "symbolic-ref", "refs/remotes/fork/HEAD", "refs/remotes/fork/main")
    _git(root, "config", "branch.feature.pushRemote", "fork")

    response = await _changes(root, COMPARISON_KIND_BRANCH_POINT)
    assert _unavailable_problem(response) is None
    # `databricks` has no default branch, so its upstream branch wins over the fork's.
    assert response.base.reference == "databricks/master"
    assert response.base.base == branch_point


async def test_the_tracked_remotes_default_branch_wins_over_its_upstream(tmp_path: Path) -> None:
    root, _branch_point = _branch_fixture(tmp_path)
    main = _git(root, "rev-parse", "main").strip()
    _git(root, "update-ref", "refs/remotes/databricks/main", main)
    _git(root, "symbolic-ref", "refs/remotes/databricks/HEAD", "refs/remotes/databricks/main")

    response = await _changes(root, COMPARISON_KIND_BRANCH_POINT)
    assert _unavailable_problem(response) is None
    assert response.base.reference == "databricks/main"
    assert response.base.base == main


def _git_appending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crafted: dict[str, str]
) -> None:
    """Put a ``git`` first on PATH that appends ``crafted`` records to real Git's output.

    :param crafted: Shell ``case`` patterns for the arguments, mapped to ``printf`` text.
    """
    real_git = shutil.which("git")
    assert real_git is not None
    bin_directory = tmp_path / "crafting-bin"
    bin_directory.mkdir()
    cases = "".join(
        f'*{pattern}*) "{real_git}" "$@" || exit $?; printf \'{text}\'; exit 0;;\n'
        for pattern, text in crafted.items()
    )
    script = bin_directory / "git"
    script.write_text(f'#!/bin/sh\ncase "$*" in\n{cases}esac\nexec "{real_git}" "$@"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")


async def test_listed_paths_outside_the_workspace_are_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crafted index or listing can't make a change describe a file beside the workspace."""
    root = _repository(tmp_path / "repo")
    _write(root / "a.txt", "changed\n")
    (tmp_path / "sibling.txt").write_text("outside")
    zero = "0" * 40
    _git_appending(
        tmp_path,
        monkeypatch,
        {
            "--raw": f":100644 100644 {zero} {zero} M\\0../sibling.txt\\0",
            "--others": "../sibling.txt\\0/etc/hosts\\0",
        },
    )

    response = await _changes(root)

    assert not response.base.HasField("unavailable"), response.base.unavailable
    assert [f.path for f in response.files] == ["a.txt"]


def test_working_metadata_never_reads_through_a_symlinked_directory(tmp_path: Path) -> None:
    """A listed path whose directory is a link outward has no metadata, not the outside file's."""
    root = tmp_path / "workspace"
    (root / "inside").mkdir(parents=True)
    (root / "inside" / "file.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("outside")
    (root / "dir").symlink_to(outside)
    (root / "link").symlink_to("inside/file.txt")

    assert working_copy.working_metadata(root, "dir/file.txt") is None
    inside = working_copy.working_metadata(root, "inside/file.txt")
    assert inside is not None and inside.size == len("inside")
    # A link itself is described by its own lstat, not its target's.
    link = working_copy.working_metadata(root, "link")
    assert link is not None and link.size == len("inside/file.txt")
