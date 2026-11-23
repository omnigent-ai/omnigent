"""Which listed entries Git ignores, through the hardened runner."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omnigent.host.workspace_git.ignored import ignored_paths
from omnigent.host.workspace_git.runner import Deadline, GitError, GitRepository
from tests.host.test_workspace_git_runner import _fake_git, _git, _repository

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the runner signals POSIX process groups"
)


def _ignoring_repository(root: Path) -> Path:
    """A repository whose ignore rules cover folders, patterns, nesting and a tracked file."""
    _repository(root)
    (root / ".gitignore").write_text("build/\n*.log\nnode_modules\n")
    (root / "build" / "out").mkdir(parents=True)
    (root / "node_modules" / "left").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / ".gitignore").write_text("generated.py\n")
    for name in ("src/main.py", "src/generated.py", "src/debug.log", "tracked.log"):
        (root / name).write_text("x")
    # A tracked file is never ignored, whatever the patterns say.
    _git(root, "add", "-f", "tracked.log")
    return root


async def test_lists_what_the_ignore_rules_cover_and_leaves_tracked_files(
    tmp_path: Path,
) -> None:
    root = _ignoring_repository(tmp_path / "repo")
    asked = [
        "build",
        "build/out",
        "node_modules",
        "node_modules/left",
        "src",
        "src/main.py",
        "src/generated.py",
        "src/debug.log",
        "tracked.log",
        "a.txt",
    ]

    ignored = await ignored_paths(root, asked, Deadline(10))

    assert ignored == {
        "build",
        "build/out",
        "node_modules",
        "node_modules/left",
        "src/generated.py",
        "src/debug.log",
    }


async def test_names_that_look_like_patterns_or_pathspec_magic_are_read_literally(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    (root / ".gitignore").write_text("*.log\n[x].ts\n")
    for name in (":(top)a.log", ":!keep.ts", "[x].ts", "x.ts", "*.ts"):
        (root / name).write_text("x")

    ignored = await ignored_paths(
        root, [":(top)a.log", ":!keep.ts", "[x].ts", "x.ts", "*.ts"], Deadline(10)
    )

    # `[x].ts` is a pattern matching `x.ts`; the file named `[x].ts` isn't ignored.
    assert ignored == {":(top)a.log", "x.ts"}


async def test_glob_characters_in_names_never_match_tracked_files(tmp_path: Path) -> None:
    """A name like ``*.log`` is itself, not a glob over the index's ``tracked.log``."""
    root = _repository(tmp_path / "repo")
    (root / ".gitignore").write_text("*.log\n")
    names = ["*.log", "q?.log", "[t]racked.log", "back\\slash.log", "tracked.log"]
    for name in names:
        (root / name).write_text("x")
    _git(root, "add", "-f", "tracked.log")

    ignored = await ignored_paths(root, names, Deadline(10))

    assert ignored == {"*.log", "q?.log", "[t]racked.log", "back\\slash.log"}


async def test_paths_are_relative_to_a_workspace_below_the_repository_root(
    tmp_path: Path,
) -> None:
    root = _ignoring_repository(tmp_path / "repo")

    ignored = await ignored_paths(root / "src", ["main.py", "generated.py"], Deadline(10))

    assert ignored == {"generated.py"}


async def test_nothing_is_ignored_outside_a_repository(tmp_path: Path) -> None:
    (tmp_path / "debug.log").write_text("x")

    assert await ignored_paths(tmp_path, ["debug.log"], Deadline(10)) == set()


async def test_asks_git_nothing_for_an_empty_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Git was asked about an empty listing")

    monkeypatch.setattr(GitRepository, "discover", fail)

    assert await ignored_paths(tmp_path, [], Deadline(10)) == set()


async def test_a_failing_check_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fake_git(tmp_path, monkeypatch, "echo 'fatal: broken' >&2; exit 128\n")

    with pytest.raises(GitError):
        await ignored_paths(root, ["debug.log"], Deadline(10))
