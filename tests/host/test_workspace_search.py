"""The host's SearchFiles: candidates, ignored files, literal scopes and failures.

Route and protocol parity with the REST search lives in ``test_workspace_service.py``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError

from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    ENTRY_TYPE_DIRECTORY,
    ENTRY_TYPE_FILE,
    FILE_SEARCH_MODE_FUZZY,
    FILE_SEARCH_MODE_SUBSTRING,
    SearchFilesRequest,
    SearchFilesResponse,
)
from omnigent.host.workspace_git import files as files_module
from omnigent.host.workspace_search import DirectoryScope, search_files
from tests.host.test_workspace_git_runner import _fake_git, _git, _repository

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the runner signals POSIX process groups"
)


@pytest.fixture(autouse=True)
def _fresh_listers() -> Iterator[None]:
    # Each test's repository gets its own untracked list.
    files_module._by_root.clear()
    yield
    files_module._by_root.clear()


async def _search(root: Path, query: str, **fields: object) -> SearchFilesResponse:
    request = SearchFilesRequest(query=query, mode=FILE_SEARCH_MODE_SUBSTRING, **fields)  # type: ignore[arg-type]
    return await search_files(root, request)


def _paths(response: SearchFilesResponse) -> list[str]:
    return [result.entry.path for result in response.results]


def _ignoring_repository(root: Path) -> Path:
    _repository(root)
    (root / "src" / "build").mkdir(parents=True)
    (root / "src" / "main.py").write_text("x")
    _git(root, "add", "src/main.py")
    (root / "src" / "new_main.py").write_text("x")
    (root / "src" / ".gitignore").write_text("build/\n")
    (root / "src" / "build" / "main.o").write_text("x")
    return root


@pytest.mark.parametrize(
    ("include_git_ignored", "expected"),
    [
        (False, ["src/main.py", "src/new_main.py"]),
        (True, ["src/build/main.o", "src/main.py", "src/new_main.py"]),
    ],
)
async def test_ignored_files_are_searched_only_when_asked(
    tmp_path: Path, include_git_ignored: bool, expected: list[str]
) -> None:
    root = _ignoring_repository(tmp_path / "repo")

    response = await _search(root, "main", include_git_ignored=include_git_ignored)

    assert _paths(response) == expected
    assert response.indexed_paths == 4  # a.txt, src/main.py, src/new_main.py, src/.gitignore
    assert not response.truncated and not response.untracked_unavailable


async def test_listed_symlinks_show_whether_or_not_ignored_files_are_searched(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    (tmp_path / "outside_target").write_text("secret bytes")
    (root / "src").mkdir()
    (root / "src" / "target.txt").write_text("12345")
    (root / "link_to_file").symlink_to("src/target.txt")
    (root / "link_to_src").symlink_to("src")
    (root / "link_outward").symlink_to(tmp_path / "outside_target")
    _git(root, "add", "src/target.txt", "link_to_file", "link_to_src", "link_outward")
    (root / "link_untracked").symlink_to("src/target.txt")

    hidden = await _search(root, "link")
    shown = await _search(root, "link", include_git_ignored=True)

    described = {
        result.entry.path: (result.entry.type, result.entry.size, result.entry.is_symlink)
        for result in hidden.results
    }
    assert described == {
        "link_outward": (ENTRY_TYPE_FILE, 0, True),  # unknown size: the target is outside
        "link_to_file": (ENTRY_TYPE_FILE, 5, True),
        "link_to_src": (ENTRY_TYPE_DIRECTORY, 0, True),
        "link_untracked": (ENTRY_TYPE_FILE, 5, True),
    }
    assert not hidden.results[0].entry.HasField("size")
    assert list(hidden.results) == list(shown.results)


@pytest.mark.parametrize("in_git", [True, False], ids=["ignored", "no-repository"])
async def test_links_only_the_walk_finds_never_describe_whats_outside(
    tmp_path: Path, in_git: bool
) -> None:
    root = _repository(tmp_path / "repo") if in_git else tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret").write_text("secret bytes")
    (root / "ignored").mkdir()
    (root / ".gitignore").write_text("ignored/\n")
    (root / "ignored" / "real.txt").write_text("12345")
    (root / "ignored" / "link_inward").symlink_to("real.txt")
    (root / "ignored" / "link_outward").symlink_to(tmp_path / "outside" / "secret")
    (root / "ignored" / "link_outward_dir").symlink_to(tmp_path / "outside")

    response = await _search(root, "link", include_git_ignored=True)

    described = {
        result.entry.path: (result.entry.type, result.entry.size, result.entry.is_symlink)
        for result in response.results
    }
    assert described == {
        "ignored/link_inward": (ENTRY_TYPE_FILE, 5, True),
        "ignored/link_outward": (ENTRY_TYPE_FILE, 0, True),
        "ignored/link_outward_dir": (ENTRY_TYPE_FILE, 0, True),
    }
    assert not any(
        result.entry.HasField("size")
        for result in response.results
        if "outward" in result.entry.path
    )


async def test_ignored_matches_cannot_crowd_out_an_unignored_one(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    (root / ".gitignore").write_text("aaa/\n")
    (root / "aaa").mkdir()
    for index in range(600):
        (root / "aaa" / f"match_{index:03}").write_text("x")
    (root / "zzz_match").write_text("x")

    hidden = await _search(root, "match", maximum_results=500)
    shown = await _search(root, "match", maximum_results=500, include_git_ignored=True)

    assert _paths(hidden) == ["zzz_match"]
    assert len(shown.results) == 500 and shown.truncated
    assert "zzz_match" not in _paths(shown)


async def test_a_subfolder_search_reports_paths_beneath_it(tmp_path: Path) -> None:
    root = _ignoring_repository(tmp_path / "repo")

    response = await _search(root, "main", path="src")

    assert _paths(response) == ["main.py", "new_main.py"]


async def test_outside_git_the_walk_finds_everything(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "todo.md").write_text("x")

    response = await _search(tmp_path, "todo")

    assert _paths(response) == ["notes/todo.md"]
    assert response.indexed_paths == 0


async def test_blank_queries_find_nothing_and_fuzzy_mode_is_unimplemented(tmp_path: Path) -> None:
    assert _paths(await _search(tmp_path, "   ")) == []
    with pytest.raises(ConnectError) as fuzzy:
        await search_files(tmp_path, SearchFilesRequest(query="x", mode=FILE_SEARCH_MODE_FUZZY))
    assert fuzzy.value.code == Code.UNIMPLEMENTED


async def test_globs_arrive_as_lists_without_comma_splitting(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    for name in ("x.py", "x.md", "x.txt", "x,y.txt"):
        (root / name).write_text("x")

    braces = await _search(root, "x", include=["*.{py,md}"])
    comma = await _search(root, "x", include=["x,y.txt"])

    assert _paths(braces) == ["x.md", "x.py"]
    assert _paths(comma) == ["x,y.txt"]


async def test_directory_scopes_are_literal_paths_matched_by_component(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    odd = "a*b?[c]{d}!e"
    for directory in (odd, "aXb", "src", "srcx", "src/vendor"):
        (root / directory).mkdir(parents=True, exist_ok=True)
        (root / directory / "hit.txt").write_text("x")

    literal = await _search(root, "hit", include_directories=[odd])
    boundary = await _search(root, "hit", include_directories=["src"])
    excluded = await _search(
        root,
        "hit",
        include_directories=["src", "src/vendor"],
        exclude_directories=["src/vendor"],
        include=["src/vendor/**", "src/*"],
    )
    ignored_too = await _search(
        root,
        "hit",
        include_directories=["src"],
        exclude_directories=["src/vendor"],
        include_git_ignored=True,
    )

    assert _paths(literal) == [f"{odd}/hit.txt"]
    assert _paths(boundary) == ["src/hit.txt", "src/vendor/hit.txt"]
    assert _paths(excluded) == ["src/hit.txt"]
    assert _paths(ignored_too) == ["src/hit.txt"]


def test_overlapping_scopes_are_reduced_to_the_outermost(tmp_path: Path) -> None:
    for directory in ("a/b/c", "a/x"):
        (tmp_path / directory).mkdir(parents=True)

    scope = DirectoryScope.parse(tmp_path, ["a/b/c", "a", "a/b", "a"], ["a/x", "a/x/y", "a/x"])

    assert scope == DirectoryScope(includes=("a",), excludes=("a/x",))
    assert DirectoryScope.parse(tmp_path, ["a", ""], []).includes == ()


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"path": "../x"}, Code.INVALID_ARGUMENT),
        ({"path": "missing"}, Code.NOT_FOUND),
        ({"path": "outward"}, Code.INVALID_ARGUMENT),
        ({"path": "fifo"}, Code.INVALID_ARGUMENT),
        ({"include_directories": ["outward"]}, Code.INVALID_ARGUMENT),
        ({"include_directories": ["inward"]}, Code.INVALID_ARGUMENT),
        ({"include_directories": ["fifo"]}, Code.INVALID_ARGUMENT),
        ({"include_directories": ["outward/inner"]}, Code.INVALID_ARGUMENT),
        ({"exclude_directories": ["/etc"]}, Code.INVALID_ARGUMENT),
    ],
)
async def test_scopes_that_leave_the_workspace_or_are_not_directories_are_refused(
    tmp_path: Path, fields: dict[str, object], code: Code
) -> None:
    root = _repository(tmp_path / "repo")
    (tmp_path / "outside" / "inner").mkdir(parents=True)
    (root / "outward").symlink_to(tmp_path / "outside")
    (root / "real").mkdir()
    (root / "inward").symlink_to("real")
    os.mkfifo(root / "fifo")

    with pytest.raises(ConnectError) as refused:
        await _search(root, "x", **fields)

    assert refused.value.code == code


async def test_missing_include_directories_just_match_nothing(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")

    assert _paths(await _search(root, "a", include_directories=["gone"])) == []


async def test_results_are_capped_with_truncated_set(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    for index in range(5):
        (root / f"hit{index}").write_text("x")

    capped = await _search(root, "hit", maximum_results=3)
    exact = await _search(root, "hit", maximum_results=5)

    assert _paths(capped) == ["hit0", "hit1", "hit2"] and capped.truncated
    assert len(exact.results) == 5 and not exact.truncated


async def test_a_tracked_listing_failure_answers_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_git(tmp_path, monkeypatch, "echo 'fatal: index file corrupt' >&2\nexit 128\n")
    (root / "a.txt").write_text("x")

    with pytest.raises(ConnectError) as refused:
        await _search(root, "a")

    assert refused.value.code == Code.UNAVAILABLE


async def test_an_untracked_listing_failure_is_reported_with_the_tracked_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_git(
        tmp_path, monkeypatch, 'case "$*" in *--others*) exit 128;; esac\nprintf "a.txt\\0"\n'
    )
    (root / "a.txt").write_text("x")

    response = await _search(root, "a")

    assert _paths(response) == ["a.txt"]
    assert response.untracked_unavailable


async def test_searching_runs_only_hardened_git_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold search, a repeat from the index and a retry neither escape the runner nor write."""
    root = _ignoring_repository(tmp_path / "repo")
    (root / "a.txt").write_text("changed")

    def state() -> dict[str, str]:
        return {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (root / ".git").rglob("*")
            if path.is_file()
        }

    started: list[tuple[str, ...]] = []
    real_popen = subprocess.Popen

    def popen(argv: list[str], *args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        # asyncio starts children through Popen too, so every child passes here.
        started.append(tuple(argv))
        if tuple(argv[:3]) != ("git", "-c", "core.fsmonitor=false"):
            raise AssertionError(f"a subprocess started outside the Git runner: {argv}")
        return real_popen(argv, *args, **kwargs)  # type: ignore[call-overload]

    def refuse(*args: object, **_kwargs: object) -> None:
        raise AssertionError(f"subprocess.run outside the Git runner: {args}")

    before = state()
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", popen)

    await _search(root, "main")
    await _search(root, "main", include_git_ignored=True)
    await _search(root, "main", retry_untracked=True)

    assert state() == before
    assert any("ls-files" in argv for argv in started)
