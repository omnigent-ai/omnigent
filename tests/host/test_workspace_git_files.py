"""Listing a workspace's files through the hardened Git runner."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from omnigent.host.workspace_git.files import WorkspaceFiles, workspace_files
from omnigent.host.workspace_git.runner import Deadline, GitError
from tests.host.test_workspace_git_runner import _fake_git, _git, _repository

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the runner signals POSIX process groups"
)


def _git_state(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / ".git").rglob("*")
        if path.is_file()
    }


async def test_lists_tracked_and_unignored_untracked_files_beneath_the_workspace(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    (root / "src" / "deep").mkdir(parents=True)
    (root / "src" / "kept.py").write_text("x")
    _git(root, "add", "src/kept.py")
    (root / "src" / "new.py").write_text("x")
    (root / "src" / "deep" / "build.log").write_text("x")
    (root / "src" / ".gitignore").write_text("*.log\n")
    _repository(root / "src" / "nested")

    listing = await WorkspaceFiles(root / "src").list_files(Deadline(10))

    assert listing is not None
    assert listing.tracked == ["kept.py"]
    assert sorted(listing.untracked or []) == [".gitignore", "nested", "new.py"]
    assert not listing.incomplete


async def test_outside_a_repository_there_is_no_listing(tmp_path: Path) -> None:
    assert await WorkspaceFiles(tmp_path).list_files(Deadline(10)) is None


async def test_a_cold_listing_writes_nothing_to_the_repository(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    (root / "untracked.txt").write_text("x")
    (root / "a.txt").write_text("changed")
    before = _git_state(root)

    files = WorkspaceFiles(root)
    await files.list_files(Deadline(10))
    await files.list_files(Deadline(10), retry_untracked=True)

    assert _git_state(root) == before


async def test_untracked_files_are_reused_briefly_unless_retried(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    files = workspace_files(root)
    assert workspace_files(root) is files
    await files.list_files(Deadline(10))
    (root / "later.txt").write_text("x")

    reused = await files.list_files(Deadline(10))
    retried = await files.list_files(Deadline(10), retry_untracked=True)

    assert reused is not None and "later.txt" not in (reused.untracked or [])
    assert retried is not None and "later.txt" in (retried.untracked or [])


async def test_an_untracked_listing_failure_leaves_the_tracked_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_git(
        tmp_path,
        monkeypatch,
        'case "$*" in *--others*) exit 128;; esac\nprintf "a.txt\\0b.txt\\0"\n',
    )

    listing = await WorkspaceFiles(root).list_files(Deadline(10))

    assert listing is not None
    assert listing.tracked == ["a.txt", "b.txt"]
    assert listing.untracked is None


async def test_a_tracked_listing_failure_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_git(tmp_path, monkeypatch, "echo 'fatal: index file corrupt' >&2\nexit 128\n")

    with pytest.raises(GitError):
        await WorkspaceFiles(root).list_files(Deadline(10))


async def test_a_listing_past_its_cap_keeps_whole_records_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.host.workspace_git.files.LISTING_OUTPUT_BYTES", 20)
    root = _fake_git(
        tmp_path, monkeypatch, 'printf "aaaa\\0bbbb\\0cccc\\0dddd\\0eeee\\0\\377\\0"\n'
    )

    listing = await WorkspaceFiles(root).list_files(Deadline(10))

    assert listing is not None
    assert listing.tracked == ["aaaa", "bbbb", "cccc", "dddd"]
    assert listing.incomplete


async def test_index_paths_that_climb_out_of_the_workspace_are_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crafted index can't make a search describe a file outside the workspace."""
    root = _fake_git(
        tmp_path, monkeypatch, 'printf "../secret.txt\\0/etc/secret\\0a/./b\\0kept.txt\\0"\n'
    )

    listing = await WorkspaceFiles(root).list_files(Deadline(10))

    assert listing is not None
    assert listing.tracked == ["kept.txt"]
