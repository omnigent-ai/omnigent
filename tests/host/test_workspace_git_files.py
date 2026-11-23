"""Listing a workspace's files through the hardened Git runner."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from pathlib import Path

import pytest

from omnigent.host.workspace_git import files as files_module
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


async def test_tracked_files_are_reused_until_the_index_changes(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    files = WorkspaceFiles(root)
    first = await files.list_files(Deadline(10))
    again = await files.list_files(Deadline(10))
    (root / "b.txt").write_text("x")
    _git(root, "add", "b.txt")
    changed = await files.list_files(Deadline(10))

    assert first is not None and again is not None and changed is not None
    assert again.tracked is first.tracked
    assert changed.tracked == ["a.txt", "b.txt"]


async def test_tracked_files_are_listed_every_time_without_an_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_git(tmp_path, monkeypatch, 'printf "a.txt\\0"\n')
    files = WorkspaceFiles(root)

    first = await files.list_files(Deadline(10))
    again = await files.list_files(Deadline(10))

    assert first is not None and again is not None
    assert again.tracked == first.tracked and again.tracked is not first.tracked


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
    monkeypatch.setattr("omnigent.host.workspace_git.files.TRACKED_LISTING_BYTES", 20)
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


async def test_a_tracked_list_past_the_other_listings_cap_is_read_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 200,000 paths of about 70 bytes: 14 MB, past the 8 MiB the other listings keep.
    root = _fake_git(
        tmp_path,
        monkeypatch,
        'case "$*" in *--others*) exit 0;; esac\n'
        "seq 1 200000 | sed 's|^|src/a-fairly-long-folder-name/another-long-folder-name/file|'"
        " | tr '\\n' '\\0'\n",
    )

    listing = await WorkspaceFiles(root).list_files(Deadline(30))

    assert listing is not None and not listing.incomplete
    assert len(listing.tracked) == 200_000


@pytest.mark.parametrize(
    "output",
    [
        b"a.txt\0src/b.py\0src/b.py\0",  # an unmerged file, once per stage
        b"a.txt\0../escape\0b.txt\0",
        b"/etc/passwd\0a.txt\0",
        b"a/./b\0a//b\0a/b/.\0a/..\0.\0..\0",
        b"./a\0a.txt\0",
        b".\0a.txt\0",
        b"..\0a.txt\0",
        b"../a\0a.txt\0",
        b"nested/\0a.txt\0",  # a nested repository ends in "/"
        b"\0a.txt\0",
        b"caf\xc3\xa9.txt\0bad\xff.txt\0ok.txt\0",
        b"a.txt\0partial/fi",  # cut off at the cap
        b"",
    ],
)
@pytest.mark.parametrize("chunk_size", [1, 3, 64])
def test_the_fast_parse_agrees_with_checking_each_record(output: bytes, chunk_size: int) -> None:
    reader = files_module._TrackedListReader(None)
    for start in range(0, len(output), chunk_size):
        reader.feed(output[start : start + chunk_size])

    assert files_module._paths(output) == files_module._checked_paths(output)
    # Streamed in chunks that split records anywhere, then in path order.
    assert reader.finish() == sorted(files_module._checked_paths(output))


def test_a_record_spanning_many_chunks_is_read_in_time_linear_in_its_length() -> None:
    chunk = b"x" * 65536

    def seconds_to_read(chunks: int) -> float:
        """The fastest of three reads of one name ``chunks`` long, as a crafted index holds."""
        fastest = float("inf")
        for _ in range(3):
            reader = files_module._TrackedListReader(None)
            started = time.perf_counter()
            for _ in range(chunks):
                reader.feed(chunk)
            reader.feed(b"\0ok.txt\0")
            fastest = min(fastest, time.perf_counter() - started)
            assert [len(path) for path in reader.finish()] == [6, chunks * len(chunk)]
        return fastest

    short, long = seconds_to_read(128), seconds_to_read(512)

    # Avoid quadratic copying of the pending record: 4x the name, not 16x the work.
    assert long < 10 * short


@pytest.mark.parametrize(
    ("output", "same"),
    [
        (b"a.txt\0b.txt\0b.txt\0", True),  # b.txt unmerged
        (b"a.txt\0", False),
        (b"a.txt\0b.txt\0c.txt\0", False),
        (b"a.txt\0c.txt\0", False),
    ],
)
@pytest.mark.parametrize("chunk_size", [1, 3, 64])
def test_a_relisting_returns_the_kept_list_only_when_its_paths_are_the_same(
    output: bytes, same: bool, chunk_size: int
) -> None:
    kept = ["a.txt", "b.txt"]
    reader = files_module._TrackedListReader(kept)
    for start in range(0, len(output), chunk_size):
        reader.feed(output[start : start + chunk_size])

    paths = reader.finish()

    assert (paths is kept) is same
    assert paths == sorted(files_module._checked_paths(output))


async def test_an_index_rewritten_with_the_same_paths_keeps_the_same_list(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    files = WorkspaceFiles(root)
    first = await files.list_files(Deadline(10))
    first_index = files._tracked[0] if files._tracked is not None else None
    _git(root, "update-index", "--index-version", "4")  # rewrites the index, same paths
    rewritten = await files.list_files(Deadline(10))
    rewritten_index = files._tracked[0] if files._tracked is not None else None
    (root / "b.txt").write_text("x")
    _git(root, "add", "b.txt")
    added = await files.list_files(Deadline(10))

    assert first is not None and rewritten is not None and added is not None
    assert rewritten_index != first_index  # listed again, not reused for an unchanged index
    assert rewritten.tracked is first.tracked
    assert added.tracked == ["a.txt", "b.txt"]


async def test_older_workspaces_lists_are_dropped_once_a_list_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files_module, "_MAX_CACHED_TRACKED_PATHS", 2)
    monkeypatch.setattr(files_module, "_by_root", files_module.OrderedDict())
    roots = []
    for name in ("older", "newer", "one", "two", "three"):
        root = _repository(tmp_path / name)  # tracks a.txt
        (root / "b.txt").write_text("x")
        _git(root, "add", "b.txt")
        roots.append(root)

    def held(loaded: list[Path]) -> list[int]:
        return [files_module._by_root[root].tracked_paths_held() for root in loaded]

    for root in roots[:2]:
        await workspace_files(root).list_files(Deadline(10))
    one_after_another = held(roots[:2])
    await asyncio.gather(*(workspace_files(root).list_files(Deadline(10)) for root in roots[2:]))

    assert one_after_another == [0, 2]  # the newest list is kept
    assert sum(held(roots)) == 2  # loads that overlap stay within the budget too
