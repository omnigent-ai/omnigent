"""Which entries of a listed directory Git ignores, through the hardened runner."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnigent.host.workspace_git.ignored import DirectoryIgnores
from omnigent.host.workspace_git.runner import Deadline, GitError, GitRepository
from tests.host.test_workspace_git_runner import _fake_git, _git, _repository

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the runner signals POSIX process groups"
)


async def _ignored(directory: Path, names: Sequence[str]) -> set[str]:
    ignores = await DirectoryIgnores.for_directory(directory, Deadline(10))
    return set() if ignores is None else await ignores.ignored(names, Deadline(10))


def _ignoring_repository(root: Path) -> Path:
    """A repository whose ignore rules cover folders, patterns, nesting and a tracked file."""
    _repository(root)
    (root / ".gitignore").write_text("build/\n*.log\nnode_modules\nvendor/\n")
    (root / "build" / "out").mkdir(parents=True)
    (root / "node_modules" / "left").mkdir(parents=True)
    (root / "vendor").mkdir()
    (root / "src").mkdir()
    (root / "src" / ".gitignore").write_text("generated.py\n")
    for name in ("src/main.py", "src/generated.py", "src/debug.log", "tracked.log"):
        (root / name).write_text("x")
    (root / "vendor" / "kept.txt").write_text("x")
    # Tracked files are never ignored, nor is the ignored folder that holds one.
    _git(root, "add", "-f", "tracked.log", "vendor/kept.txt")
    return root


async def test_lists_what_the_ignore_rules_cover_and_leaves_tracked_files(
    tmp_path: Path,
) -> None:
    root = _ignoring_repository(tmp_path / "repo")

    ignored = await _ignored(
        root, ["build", "node_modules", "vendor", "src", "tracked.log", "a.txt"]
    )

    assert ignored == {"build", "node_modules"}


async def test_paths_below_the_directory_answer_like_its_own_entries(tmp_path: Path) -> None:
    """A folder chain's inner folders are asked as paths, with the tracked-file exception."""
    root = _ignoring_repository(tmp_path / "repo")
    (root / "src" / "gen" / "out").mkdir(parents=True)
    (root / "src" / ".gitignore").write_text("generated.py\ngen/\n")
    (root / "src" / "gen" / "kept" / "schema.json").parent.mkdir()
    (root / "src" / "gen" / "kept" / "schema.json").write_text("x")
    _git(root, "add", "-f", "src/gen/kept/schema.json")

    ignored = await _ignored(
        root, ["src/gen", "src/gen/out", "src/gen/kept", "src/main.py", "vendor"]
    )

    # src/gen holds a tracked file, so only its untracked folder is ignored.
    assert ignored == {"src/gen/out"}


async def test_escaped_and_negated_patterns_match_the_real_names(tmp_path: Path) -> None:
    """Rules that escape glob characters see the name as it is, unescaped."""
    root = _repository(tmp_path / "repo")
    (root / ".gitignore").write_text("*.log\n!keep\\[1\\].log\nroute\\[id\\]\n")
    for name in ("keep[1].log", "other.log", "route[id]", "routei"):
        (root / name).write_text("x")

    ignored = await _ignored(root, ["keep[1].log", "other.log", "route[id]", "routei"])

    assert ignored == {"other.log", "route[id]"}


async def test_names_that_look_like_patterns_or_pathspec_magic_are_read_literally(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    (root / ".gitignore").write_text("*.log\n[x].ts\n")
    names = [":(top)a.log", ":!keep.ts", "[x].ts", "x.ts", "*.ts"]
    for name in names:
        (root / name).write_text("x")

    ignored = await _ignored(root, names)

    # `[x].ts` is a pattern matching `x.ts`; the file named `[x].ts` isn't ignored.
    assert ignored == {":(top)a.log", "x.ts"}


async def test_glob_characters_in_names_never_match_tracked_files(tmp_path: Path) -> None:
    """A name like ``*.log`` is itself, not a glob over the index's ``tracked.log``."""
    root = _repository(tmp_path / "repo")
    (root / ".gitignore").write_text("*.log\na\\[b\\].txt\n")
    names = ["*.log", "q?.log", "[t]racked.log", "back\\slash.log", "tracked.log", "a[b].txt"]
    for name in [*names, "ab.txt"]:
        (root / name).write_text("x")
    _git(root, "add", "-f", "tracked.log", "ab.txt")

    ignored = await _ignored(root, [*names, "ab.txt"])

    assert ignored == {"*.log", "q?.log", "[t]racked.log", "back\\slash.log", "a[b].txt"}


async def test_a_submodule_answers_with_its_own_rules(tmp_path: Path) -> None:
    outer = _repository(tmp_path / "outer")
    inner = _repository(outer / "inner")
    (inner / ".gitignore").write_text("*.tmp\n")
    (inner / "scratch.tmp").write_text("x")
    (inner / "kept.txt").write_text("x")
    # The outer repository records `inner` as a gitlink, so it can't answer inside it.
    _git(inner, "add", ".")
    _git(inner, "commit", "-qm", "inner")
    _git(outer, "add", "inner")

    assert await _ignored(inner, ["scratch.tmp", "kept.txt", ".gitignore"]) == {"scratch.tmp"}


async def test_nothing_is_ignored_outside_a_repository(tmp_path: Path) -> None:
    (tmp_path / "debug.log").write_text("x")

    assert await DirectoryIgnores.for_directory(tmp_path, Deadline(10)) is None


async def test_asks_git_nothing_for_an_empty_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Git was asked about an empty listing")

    ignores = await DirectoryIgnores.for_directory(_repository(tmp_path / "repo"), Deadline(10))
    assert ignores is not None
    monkeypatch.setattr(GitRepository, "run_unchecked", fail)

    assert await ignores.ignored([], Deadline(10)) == set()


async def test_a_failing_check_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fake_git(tmp_path, monkeypatch, "echo 'fatal: broken' >&2; exit 128\n")

    with pytest.raises(GitError):
        await _ignored(root, ["debug.log"])
