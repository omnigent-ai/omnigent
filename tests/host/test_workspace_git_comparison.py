"""Revision comparisons, branch commits and refs, against real temporary repositories."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError

from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    CHANGE_STATUS_ADDED,
    COMPARISON_FIELD_BASE,
    COMPARISON_FIELD_BOTH,
    COMPARISON_FIELD_HEAD,
    COMPARISON_KIND_REVISIONS,
    COMPARISON_PROBLEM_BASE_NOT_FOUND,
    COMPARISON_PROBLEM_HEAD_NOT_FOUND,
    COMPARISON_PROBLEM_INVALID_REVISION,
    COMPARISON_PROBLEM_NO_COMMON_HISTORY,
    Comparison,
    ListBranchCommitsRequest,
    ListBranchCommitsResponse,
    ListRepositoryRefsRequest,
    ListWorkingCopyChangesRequest,
    ListWorkingCopyChangesResponse,
    ReadBaseFileRequest,
)
from omnigent.host.workspace_git import comparison
from omnigent.host.workspace_git.comparison import (
    EMPTY_TREES,
    MAXIMUM_COMMIT_BODY_BYTES,
    list_branch_commits,
    list_repository_refs,
)
from omnigent.host.workspace_git.working_copy import list_working_copy_changes, read_base_file
from tests.host.test_workspace_git_runner import _git, _repository
from tests.host.test_workspace_git_working_copy import (
    _add_remote,
    _commit,
    _file,
    _head,
    _paths,
    _snapshot,
    _write,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the runner signals POSIX process groups"
)

_EMPTY_TREE = EMPTY_TREES[0]


def _branches(tmp_path: Path) -> Path:
    """``main`` with a.txt, tagged v1; ``feature`` adds b.txt and edits a.txt; ``main`` moves on.

    Checked out on ``feature``.
    """
    root = _repository(tmp_path / "repo")
    _git(root, "tag", "v1")
    _git(root, "checkout", "-q", "-b", "feature")
    _write(root / "a.txt", "a\nTWO\n")
    _write(root / "b.txt", "bee\n")
    _commit(root, "feature work")
    _git(root, "checkout", "-q", "main")
    _write(root / "c.txt", "sea\n")
    _commit(root, "main moves on")
    _git(root, "checkout", "-q", "feature")
    return root


def _with_origin_default_branch(root: Path) -> None:
    """``origin`` as a clone configures it, its default branch at the fork point ``v1``."""
    _add_remote(root, "origin")
    _git(root, "update-ref", "refs/remotes/origin/main", _git(root, "rev-parse", "v1").strip())
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def _commit_on_head(root: Path, message: str) -> None:
    """Add one commit with HEAD's tree, without running hooks, and move HEAD to it."""
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_COMMITTER_EMAIL": "dev@example.com",
    }
    tree = _git(root, "rev-parse", "HEAD^{tree}").strip()
    commit = subprocess.run(
        ["git", "-C", str(root), "commit-tree", tree, "-p", _head(root), "-m", message],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    _git(root, "update-ref", "HEAD", commit)


async def _compare(root: Path, base: str, head: str = "") -> ListWorkingCopyChangesResponse:
    return await list_working_copy_changes(
        root,
        ListWorkingCopyChangesRequest(
            comparison=Comparison(
                kind=COMPARISON_KIND_REVISIONS, base_revision=base, head_revision=head
            )
        ),
    )


async def _branch_commits(root: Path, base: str) -> ListBranchCommitsResponse:
    return await list_branch_commits(root, ListBranchCommitsRequest(base_revision=base))


def _subjects(response: ListBranchCommitsResponse) -> list[str]:
    return [commit.subject for commit in response.commits]


def _problem(response: ListWorkingCopyChangesResponse) -> tuple[int, int]:
    unavailable = response.base.unavailable
    return unavailable.problem, unavailable.field


async def test_a_base_and_the_working_copy_compare_from_their_merge_base(tmp_path: Path) -> None:
    root = _branches(tmp_path)
    _write(root / "untracked.txt", "new\n")
    _write(root / "b.txt", "bee\nedited\n")

    response = await _compare(root, "main")
    assert not response.base.HasField("unavailable")
    base = response.base
    # The merge base is where feature left main, not main's newer tip.
    assert base.base == _git(root, "rev-parse", "v1").strip()
    assert base.merge_base == base.base
    assert base.head == _head(root)
    assert base.base_display == "main"
    assert base.head_display == ""
    assert base.head_is_working_copy
    # Committed, edited and untracked files; main's own c.txt isn't a change here.
    assert _paths(response) == ["a.txt", "b.txt", "untracked.txt"]
    assert not _file(response, "b.txt").HasField("committed")


async def test_a_committed_head_lists_only_its_commits_and_reads_both_blobs(
    tmp_path: Path,
) -> None:
    root = _branches(tmp_path)
    (root / "d.bin").write_bytes(b"\0\1\2 not text")
    _commit(root, "feature adds a binary file")
    _write(root / "untracked.txt", "new\n")
    _write(root / "a.txt", "dirty working copy\n")

    response = await _compare(root, "main", "feature")
    assert not response.base.HasField("unavailable")
    base = response.base
    assert base.merge_base == _git(root, "rev-parse", "v1").strip()
    assert base.head == _git(root, "rev-parse", "feature").strip()
    assert base.head_display == "feature"
    assert not base.head_is_working_copy
    # Neither the untracked file nor the working copy's edit belongs to the commits.
    assert _paths(response) == ["a.txt", "b.txt", "d.bin"]
    added = _file(response, "b.txt")
    assert added.status == CHANGE_STATUS_ADDED
    assert not added.HasField("working")
    assert (added.lines_added, added.lines_removed) == (1, 0) and not added.binary
    added_binary = _file(response, "d.bin")
    assert added_binary.status == CHANGE_STATUS_ADDED
    assert added_binary.binary and not added_binary.HasField("lines_added")
    modified = _file(response, "a.txt")
    assert (modified.lines_added, modified.lines_removed) == (1, 0)
    assert modified.committed.size == len(b"a\nTWO\n")

    before_responses = [
        chunk
        async for chunk in read_base_file(
            root,
            ReadBaseFileRequest(
                commit=base.merge_base,
                path="a.txt",
                object=modified.base_object,
                maximum_bytes=0,
            ),
        )
    ]
    before = b"".join(chunk.data for chunk in before_responses)
    after_responses = [
        chunk
        async for chunk in read_base_file(
            root,
            ReadBaseFileRequest(
                commit=base.head,
                path="a.txt",
                object=modified.committed.object,
                maximum_bytes=0,
            ),
        )
    ]
    after = b"".join(chunk.data for chunk in after_responses)
    assert before == b"a\n"
    assert after == b"a\nTWO\n", "the head's blob, not the dirty working file"


async def test_an_empty_base_means_the_default_branch_else_head(tmp_path: Path) -> None:
    root = _branches(tmp_path)
    # No remote, so no default branch: the base is HEAD itself.
    response = await _compare(root, "")
    assert response.base.base_display == "HEAD"
    assert response.base.merge_base == _head(root)
    assert not response.files


async def test_an_empty_base_is_the_detected_default_branch_even_when_a_tag_shares_its_name(
    tmp_path: Path,
) -> None:
    root = _branches(tmp_path)
    _with_origin_default_branch(root)
    # A tag named like the default branch's short name, at feature's tip.
    _git(root, "tag", "origin/main", "feature")

    response = await _compare(root, "")
    assert not response.base.HasField("unavailable")
    assert response.base.base_display == "origin/main"
    assert (
        response.base.reference_commit
        == _git(root, "rev-parse", "refs/remotes/origin/main").strip()
    )
    assert response.base.merge_base == _git(root, "rev-parse", "v1").strip()
    assert _paths(response) == ["a.txt", "b.txt"]


async def test_refs_leave_out_only_remote_heads_and_name_the_default_branch(
    tmp_path: Path,
) -> None:
    root = _branches(tmp_path)
    _with_origin_default_branch(root)
    _git(root, "branch", "topic/HEAD", "main")
    _git(root, "tag", "release/HEAD", "main")

    refs = await list_repository_refs(root, ListRepositoryRefsRequest())
    assert list(refs.refs) == [
        "feature",
        "main",
        "topic/HEAD",
        "origin/main",
        "release/HEAD",
        "v1",
    ]
    assert not refs.truncated
    assert refs.default_branch_ref == "origin/main"


async def test_refs_list_branches_and_tags_without_a_default_branch(tmp_path: Path) -> None:
    root = _branches(tmp_path)
    refs = await list_repository_refs(root, ListRepositoryRefsRequest())
    assert list(refs.refs) == ["feature", "main", "v1"]
    assert not refs.truncated
    assert refs.default_branch_ref == ""


async def test_refs_outside_a_repository_are_a_precondition_failure(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ConnectError) as refused:
        await list_repository_refs(outside, ListRepositoryRefsRequest())
    assert refused.value.code == Code.FAILED_PRECONDITION


@pytest.mark.parametrize(
    ("base", "head", "field"),
    [
        ("--output=leaked.txt", "", COMPARISON_FIELD_BASE),
        ("main", "-p", COMPARISON_FIELD_HEAD),
        ("main\nHEAD", "", COMPARISON_FIELD_BASE),
        ("main\x00HEAD", "", COMPARISON_FIELD_BASE),
    ],
    ids=["option-base", "option-head", "newline", "nul"],
)
async def test_revisions_that_could_be_options_never_reach_git(
    tmp_path: Path, base: str, head: str, field: int
) -> None:
    root = _branches(tmp_path)
    response = await _compare(root, base, head)
    assert _problem(response) == (COMPARISON_PROBLEM_INVALID_REVISION, field)
    assert not response.files
    assert not (root / "leaked.txt").exists(), "git ran with the option"


@pytest.mark.parametrize(
    ("base", "head", "expected"),
    [
        ("nope", "", (COMPARISON_PROBLEM_BASE_NOT_FOUND, COMPARISON_FIELD_BASE)),
        ("main", "nope", (COMPARISON_PROBLEM_HEAD_NOT_FOUND, COMPARISON_FIELD_HEAD)),
        ("nope", "nada", (COMPARISON_PROBLEM_BASE_NOT_FOUND, COMPARISON_FIELD_BOTH)),
    ],
    ids=["unknown-base", "unknown-head", "both-unknown"],
)
async def test_unknown_revisions_name_the_field_they_are_in(
    tmp_path: Path, base: str, head: str, expected: tuple[int, int]
) -> None:
    root = _branches(tmp_path)
    assert _problem(await _compare(root, base, head)) == expected


async def test_revisions_with_no_shared_commit_say_so_for_both(tmp_path: Path) -> None:
    root = _branches(tmp_path)
    _git(root, "checkout", "-q", "--orphan", "unrelated")
    _git(root, "rm", "-rq", "--cached", ".")
    _write(root / "z.txt", "zed\n")
    _commit(root, "unrelated root")
    assert _problem(await _compare(root, "main", "unrelated")) == (
        COMPARISON_PROBLEM_NO_COMMON_HISTORY,
        COMPARISON_FIELD_BOTH,
    )


async def test_a_root_commit_from_unrelated_history_is_listed_and_compares_with_the_empty_tree(
    tmp_path: Path,
) -> None:
    root = _branches(tmp_path)
    _git(root, "checkout", "-q", "--orphan", "imported")
    _git(root, "rm", "-q", "-rf", ".")
    _write(root / "imported.txt", "from elsewhere\n")
    _commit(root, "imported root")
    imported_root = _head(root)
    _git(root, "checkout", "-q", "feature")
    _git(
        root,
        "-c",
        "user.name=Dev",
        "-c",
        "user.email=dev@example.com",
        "merge",
        "-q",
        "--allow-unrelated-histories",
        "-m",
        "merge imported",
        "imported",
    )

    listed = await _branch_commits(root, "main")
    roots = sorted((commit.subject, commit.root) for commit in listed.commits)
    assert roots == [("feature work", False), ("imported root", True), ("merge imported", False)]

    # The root's parent doesn't exist here, so the base revision is unknown.
    parent = await _compare(root, f"{imported_root}~1", imported_root)
    assert _problem(parent) == (COMPARISON_PROBLEM_BASE_NOT_FOUND, COMPARISON_FIELD_BASE)

    # A head with parents can't be shown against the empty tree, as if all of it were added.
    for head in (_head(root), _git(root, "rev-parse", "HEAD^1").strip()):
        refused = await _compare(root, _EMPTY_TREE, head)
        assert not refused.files
        assert _problem(refused) == (COMPARISON_PROBLEM_BASE_NOT_FOUND, COMPARISON_FIELD_BASE)

    changes = await _compare(root, _EMPTY_TREE, imported_root)
    assert _paths(changes) == ["imported.txt"]
    added = _file(changes, "imported.txt")
    assert added.status == CHANGE_STATUS_ADDED
    assert changes.base.merge_base == _EMPTY_TREE
    chunks = [
        chunk
        async for chunk in read_base_file(
            root,
            ReadBaseFileRequest(
                commit=imported_root,
                path="imported.txt",
                object=added.committed.object,
                maximum_bytes=0,
            ),
        )
    ]
    assert b"".join(chunk.data for chunk in chunks) == b"from elsewhere\n"


async def test_a_shallow_clones_boundary_commit_is_not_shown_against_the_empty_tree(
    tmp_path: Path,
) -> None:
    source = _branches(tmp_path)
    clone = tmp_path / "shallow"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth",
            "1",
            "--branch",
            "feature",
            f"file://{source}",
            str(clone),
        ],
        check=True,
        capture_output=True,
    )
    # The clone hides the tip's parent, though the commit still names it.
    assert _git(clone, "log", "-1", "--format=%P").strip() == ""
    refused = await _compare(clone, _EMPTY_TREE, _head(clone))
    assert not refused.files
    assert _problem(refused) == (COMPARISON_PROBLEM_BASE_NOT_FOUND, COMPARISON_FIELD_BASE)


async def test_branch_commits_run_from_the_default_branchs_merge_base_newest_first(
    tmp_path: Path,
) -> None:
    root = _branches(tmp_path)
    _with_origin_default_branch(root)
    _write(root / "more.txt", "more\n")
    _commit(root, "more feature work")

    listed = await _branch_commits(root, "")
    assert _subjects(listed) == ["more feature work", "feature work"]
    assert not listed.truncated
    newest = listed.commits[0]
    assert newest.object == _head(root)
    assert newest.author_name == "Dev"
    assert newest.author_time_unix > 0
    assert listed.base.base_display == "origin/main"
    assert listed.base.merge_base == _git(root, "rev-parse", "v1").strip()
    # A named base counts from its own merge base.
    assert _subjects(await _branch_commits(root, "feature~1")) == ["more feature work"]


async def test_branch_commits_follow_a_detached_head_and_are_empty_on_the_base_itself(
    tmp_path: Path,
) -> None:
    root = _branches(tmp_path)
    _git(root, "checkout", "-q", "--detach", "feature")
    assert _subjects(await _branch_commits(root, "main")) == ["feature work"]
    _git(root, "checkout", "-q", "main")
    on_main = await _branch_commits(root, "main")
    assert not on_main.commits
    assert not on_main.base.HasField("unavailable")


async def test_branch_commits_carry_each_messages_body_after_its_subject(tmp_path: Path) -> None:
    root = _branches(tmp_path)
    _commit(root, "Explain the parser\n\n  Why it reads ahead.\n\nA second paragraph.\n\n")
    _commit(root, "Only a subject")
    # A subject or body may hold the listing's field separator; it stays where it was.
    _commit(root, "Odd\x1fsubject\n\nbefore\x1fafter")

    odd, only_subject, explained, feature = (await _branch_commits(root, "main")).commits
    assert explained.subject == "Explain the parser"
    assert explained.body == "  Why it reads ahead.\n\nA second paragraph."
    assert only_subject.body == ""
    assert odd.subject == "Odd\x1fsubject"
    assert odd.body == "before\x1fafter"
    assert feature.subject == "feature work"


async def test_a_long_commit_body_is_cut_at_a_character_within_the_limit(
    tmp_path: Path,
) -> None:
    root = _branches(tmp_path)
    # Three-byte characters, so the limit falls inside one.
    body = "€" * (MAXIMUM_COMMIT_BODY_BYTES // 3 + 10)
    _commit(root, f"Long\n\n{body}")

    long_body = (await _branch_commits(root, "main")).commits[0].body
    assert len(long_body.encode()) <= MAXIMUM_COMMIT_BODY_BYTES
    # It ends in an ellipsis, after as many whole characters as fit before it.
    assert long_body.endswith("…")
    kept = long_body.removesuffix("…")
    assert body.startswith(kept)
    assert len(kept.encode()) > MAXIMUM_COMMIT_BODY_BYTES - 3 - 3


async def test_branch_commits_too_long_to_list_with_their_bodies_still_list_their_subjects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _branches(tmp_path)
    # A small listing limit, so a modest body is past it and the subjects alone fit.
    listing_limit = 16 * 1024
    monkeypatch.setattr(comparison, "LISTING_OUTPUT_BYTES", listing_limit)
    _commit(root, f"Vendor the log\n\n{'x' * (listing_limit + 1024)}\n")

    listed = await _branch_commits(root, "main")
    assert [commit.subject for commit in listed.commits] == ["Vendor the log", "feature work"]
    assert all(commit.body == "" for commit in listed.commits)


async def test_branch_commits_stop_at_the_limit_and_say_so(tmp_path: Path) -> None:
    root = _branches(tmp_path)
    for index in range(200):
        _commit_on_head(root, f"step {index}")

    listed = await _branch_commits(root, "main")
    assert len(listed.commits) == 200
    assert listed.truncated
    assert listed.commits[0].subject == "step 199"


async def test_branch_commits_report_a_base_that_isnt_there_or_could_be_an_option(
    tmp_path: Path,
) -> None:
    root = _branches(tmp_path)
    for base, expected in [
        ("no-such-branch", COMPARISON_PROBLEM_BASE_NOT_FOUND),
        ("--output=leaked.txt", COMPARISON_PROBLEM_INVALID_REVISION),
    ]:
        listed = await _branch_commits(root, base)
        assert not listed.commits, base
        assert listed.base.unavailable.problem == expected, base
        assert listed.base.unavailable.field == COMPARISON_FIELD_BASE, base
    assert not (root / "leaked.txt").exists()


async def test_revision_comparisons_and_listings_only_read_the_repository(tmp_path: Path) -> None:
    root = _branches(tmp_path)
    _with_origin_default_branch(root)
    _write(root / "untracked.txt", "new\n")
    before = _snapshot(root / ".git")

    await _compare(root, "main")
    await _compare(root, "main", "feature")
    await _branch_commits(root, "main")
    await list_repository_refs(root, ListRepositoryRefsRequest())

    assert _snapshot(root / ".git") == before


async def test_a_head_compared_with_itself_has_no_changes(tmp_path: Path) -> None:
    root = _branches(tmp_path)
    response = await _compare(root, "feature", "feature")
    assert not response.base.HasField("unavailable")
    assert not response.files
