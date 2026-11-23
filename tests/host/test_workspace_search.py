"""The host's SearchFiles and SearchText: candidates, ignored files, literal scopes and failures.

Route and protocol parity with the REST search lives in ``test_workspace_service.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
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
    FILE_SEARCH_MODE_UNSPECIFIED,
    SearchFilesRequest,
    SearchFilesResponse,
    SearchTextRequest,
    SearchTextResponse,
)
from omnigent.host import workspace_fuzzy, workspace_search
from omnigent.host.workspace_git import files as files_module
from omnigent.host.workspace_search import DirectoryScope, search_files, search_text
from tests.host.test_workspace_git_runner import _alive, _fake_git, _git, _repository

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the runner signals POSIX process groups"
)


@pytest.fixture(autouse=True)
def _fresh_listers() -> Iterator[None]:
    # Each test's repository gets its own listings and ranked file lists.
    files_module._by_root.clear()
    workspace_search._corpora = workspace_fuzzy.CorpusCache()
    yield
    files_module._by_root.clear()


async def _search(root: Path, query: str, **fields: object) -> SearchFilesResponse:
    request = SearchFilesRequest(query=query, mode=FILE_SEARCH_MODE_SUBSTRING, **fields)  # type: ignore[arg-type]
    return await search_files(root, request)


async def _fuzzy(root: Path, query: str, **fields: object) -> SearchFilesResponse:
    request = SearchFilesRequest(query=query, mode=FILE_SEARCH_MODE_FUZZY, **fields)  # type: ignore[arg-type]
    return await search_files(root, request)


async def _search_in(root: Path, mode: int, query: str, **fields: object) -> SearchFilesResponse:
    request = SearchFilesRequest(query=query, mode=mode, **fields)  # type: ignore[arg-type]
    return await search_files(root, request)


# Both ways of finding files must consider the same candidates.
_BOTH_MODES = pytest.mark.parametrize(
    "mode", [FILE_SEARCH_MODE_SUBSTRING, FILE_SEARCH_MODE_FUZZY], ids=["substring", "fuzzy"]
)


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


@_BOTH_MODES
@pytest.mark.parametrize(
    ("include_git_ignored", "expected"),
    [
        (False, ["src/main.py", "src/new_main.py"]),
        (True, ["src/build/main.o", "src/main.py", "src/new_main.py"]),
    ],
)
async def test_ignored_files_are_searched_only_when_asked(
    tmp_path: Path, mode: int, include_git_ignored: bool, expected: list[str]
) -> None:
    root = _ignoring_repository(tmp_path / "repo")

    response = await _search_in(root, mode, "main", include_git_ignored=include_git_ignored)

    assert sorted(_paths(response)) == expected
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


@_BOTH_MODES
async def test_outside_git_the_walk_finds_everything(tmp_path: Path, mode: int) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "todo.md").write_text("x")
    (tmp_path / "readme.md").write_text("x")

    response = await _search_in(tmp_path, mode, "todo")

    assert _paths(response) == ["notes/todo.md"]
    assert response.indexed_paths == 0


async def test_blank_substring_queries_find_nothing(tmp_path: Path) -> None:
    assert _paths(await _search(tmp_path, "   ")) == []


@pytest.mark.parametrize("mode", [FILE_SEARCH_MODE_FUZZY, FILE_SEARCH_MODE_UNSPECIFIED])
async def test_fuzzy_mode_is_unimplemented_where_paths_cant_be_ranked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    monkeypatch.setattr(workspace_fuzzy, "_frizbee", lambda: None)

    with pytest.raises(ConnectError) as fuzzy:
        await search_files(tmp_path, SearchFilesRequest(query="x", mode=mode))  # type: ignore[arg-type]

    assert fuzzy.value.code == Code.UNIMPLEMENTED


def _project(root: Path) -> Path:
    _repository(root)
    (root / "web" / "electron").mkdir(parents=True)
    (root / "web" / "electron" / "workspaceFastTrack.ts").write_text("x")
    (root / "web" / "fast").mkdir()
    (root / "web" / "fast" / "tracks.ts").write_text("x")
    (root / "docs").mkdir()
    (root / "docs" / "workspace.md").write_text("x")
    _git(root, "add", ".")  # the index alone decides what's tracked
    (root / "web" / "electron" / "workspaceFastTrack.test.ts").write_text("x")  # untracked
    return root


@pytest.mark.parametrize("mode", [FILE_SEARCH_MODE_FUZZY, FILE_SEARCH_MODE_UNSPECIFIED])
async def test_fuzzy_queries_rank_files_best_first_with_highlights(
    tmp_path: Path, mode: int
) -> None:
    root = _project(tmp_path / "repo")

    response = await _search_in(root, mode, "wsft")

    assert _paths(response)[:2] == [
        "web/electron/workspaceFastTrack.ts",
        "web/electron/workspaceFastTrack.test.ts",
    ]
    best = response.results[0]
    assert best.score >= response.results[-1].score > 0
    path = best.entry.path
    assert "".join(path[index] for index in best.matched_indices).lower() == "wsft"
    assert all(result.entry.type == ENTRY_TYPE_FILE for result in response.results)
    assert response.indexed_paths == 5 and not response.truncated


async def test_a_fuzzy_query_past_the_longest_a_search_takes_is_refused(tmp_path: Path) -> None:
    root = _project(tmp_path / "repo")

    assert _paths(await _fuzzy(root, "w" * 4096)) == []
    with pytest.raises(ConnectError) as refused:
        await _fuzzy(root, "w" * 4097)

    assert refused.value.code == Code.INVALID_ARGUMENT


async def test_a_blank_fuzzy_query_lists_files_in_path_order(tmp_path: Path) -> None:
    root = _project(tmp_path / "repo")

    every = await _fuzzy(root, "  ")
    capped = await _fuzzy(root, "", maximum_results=2)

    assert _paths(every) == [
        "a.txt",
        "docs/workspace.md",
        "web/electron/workspaceFastTrack.test.ts",
        "web/electron/workspaceFastTrack.ts",
        "web/fast/tracks.ts",
    ]
    assert all(result.score == 0 and not result.matched_indices for result in every.results)
    assert not every.truncated
    assert _paths(capped) == ["a.txt", "docs/workspace.md"] and capped.truncated


async def test_fuzzy_results_keep_to_the_path_scopes_and_globs(tmp_path: Path) -> None:
    root = _project(tmp_path / "repo")

    beneath = await _fuzzy(root, "ts", path="web")
    scoped = await _fuzzy(root, "", include_directories=["web"], exclude_directories=["web/fast"])
    globbed = await _fuzzy(root, "", include=["*.md"])

    assert set(_paths(beneath)) == {
        "electron/workspaceFastTrack.ts",
        "electron/workspaceFastTrack.test.ts",
        "fast/tracks.ts",
    }
    assert _paths(scoped) == [
        "web/electron/workspaceFastTrack.test.ts",
        "web/electron/workspaceFastTrack.ts",
    ]
    assert _paths(globbed) == ["docs/workspace.md"]


async def test_fuzzy_mode_skips_deleted_files_and_never_describes_outside_links(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    (tmp_path / "secret.txt").write_text("secret bytes")
    (root / "gone.txt").write_text("x")
    (root / "link_out.txt").symlink_to(tmp_path / "secret.txt")
    (root / "sub").mkdir()
    (root / "sub" / "x.txt").write_text("x")
    (root / "linked_dir").symlink_to("sub")
    _git(root, "add", "gone.txt", "link_out.txt", "linked_dir", "sub/x.txt")
    (root / "gone.txt").unlink()

    response = await _fuzzy(root, "")

    described = {r.entry.path: (r.entry.size, r.entry.is_symlink) for r in response.results}
    # The deleted file and the link to a directory are left out.
    assert described == {"a.txt": (2, False), "link_out.txt": (0, True), "sub/x.txt": (1, False)}
    assert not response.results[1].entry.HasField("size")


async def test_a_new_commit_is_ranked_on_the_next_query(tmp_path: Path) -> None:
    root = _project(tmp_path / "repo")
    assert "lib/ranking.py" not in _paths(await _fuzzy(root, "rank"))
    (root / "lib").mkdir()
    (root / "lib" / "ranking.py").write_text("x")
    _git(root, "add", "lib/ranking.py")

    assert _paths(await _fuzzy(root, "rank")) == ["lib/ranking.py"]


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


async def _search_text(root: Path, query: str, **fields: object) -> list[SearchTextResponse]:
    request = SearchTextRequest(query=query, **fields)  # type: ignore[arg-type]
    return [response async for response in search_text(root, request)]


def _lines(responses: list[SearchTextResponse]) -> list[str]:
    return [f"{m.path}:{m.line}" for response in responses for m in response.matches]


needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")


@needs_rg
async def test_text_matches_arrive_in_batches_then_done(tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_text("needle\n" * 250)

    responses = await _search_text(tmp_path, "needle")

    assert [len(r.matches) for r in responses] == [100, 100, 50]
    assert [r.done for r in responses] == [False, False, True]
    assert not responses[-1].truncated


@needs_rg
async def test_text_scopes_are_literal_and_excludes_win(tmp_path: Path) -> None:
    odd = "a*b?[c]{d}!e"
    for directory in (odd, "aXb", "src", "srcx", "src/vendor", "other/src/vendor"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
        (tmp_path / directory / "hit.txt").write_text("needle\n")

    literal = await _search_text(tmp_path, "needle", exclude_directories=[odd, "src", "other"])
    scoped = await _search_text(
        tmp_path,
        "needle",
        include_directories=["src", "src/vendor", "src"],
        exclude_directories=["src/vendor"],
        include="src/vendor/**, src/*",
    )

    assert sorted(_lines(literal)) == ["aXb/hit.txt:1", "srcx/hit.txt:1"]
    assert _lines(scoped) == ["src/hit.txt:1"]


@needs_rg
async def test_text_flags_reach_ripgrep(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("needle\n")
    (tmp_path / "words.txt").write_text("needles\nneedle\n")

    plain = await _search_text(tmp_path, "needle")
    word = await _search_text(tmp_path, "needle", whole_word=True)
    ignored = await _search_text(tmp_path, "needle", whole_word=True, include_git_ignored=True)

    assert _lines(plain) == ["words.txt:1", "words.txt:2"]
    assert _lines(word) == ["words.txt:2"]
    assert sorted(_lines(ignored)) == ["ignored.txt:1", "words.txt:2"]


@needs_rg
async def test_a_missing_include_directory_finds_nothing(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle\n")

    responses = await _search_text(tmp_path, "needle", include_directories=["gone"])

    assert [(len(r.matches), r.done) for r in responses] == [(0, True)]


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"include_directories": ["outward"]}, Code.INVALID_ARGUMENT),
        ({"include_directories": ["fifo"]}, Code.INVALID_ARGUMENT),
        ({"include_directories": ["../x"]}, Code.INVALID_ARGUMENT),
        ({"exclude_directories": ["/etc"]}, Code.INVALID_ARGUMENT),
        ({"query": "needle\nsecond"}, Code.INVALID_ARGUMENT),
    ],
)
async def test_text_scopes_and_queries_are_refused_before_ripgrep_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fields: dict[str, object], code: Code
) -> None:
    (tmp_path / "outside").mkdir()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "outward").symlink_to(tmp_path / "outside")
    os.mkfifo(root / "fifo")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a search process started")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    with pytest.raises(ConnectError) as refused:
        await _search_text(root, **{"query": "needle", **fields})  # type: ignore[arg-type]

    assert refused.value.code == code


@needs_rg
async def test_an_invalid_pattern_is_the_users_to_fix(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle\n")

    with pytest.raises(ConnectError) as refused:
        await _search_text(tmp_path, "[", regular_expression=True)

    assert refused.value.code == Code.INVALID_ARGUMENT
    assert "regex parse error" in refused.value.message


async def test_closing_a_text_search_early_stops_ripgrep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that goes away leaves no ripgrep or descendant running."""
    pids = tmp_path / "pids"
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    record = (
        '{"type":"match","data":{"path":{"text":"./a.txt"},"lines":{"text":"needle\\n"},'
        '"line_number":1,"submatches":[{"start":0,"end":6}]}}'
    )
    fake = bin_directory / "rg"
    fake.write_text(
        "#!/bin/sh\n"
        f"echo $$ >> {pids}\nsleep 60 & echo $! >> {pids}\n"
        f"i=0; while [ $i -lt 150 ]; do printf '%s\\n' '{record}'; i=$((i+1)); done\nwait\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
    root = tmp_path / "workspace"
    root.mkdir()

    stream = search_text(root, SearchTextRequest(query="needle"))
    first = await anext(stream)
    await stream.aclose()

    assert len(first.matches) == 100
    from omnigent.host.workspace_git import runner

    await asyncio.gather(*runner._cleanups)
    leftover = [int(pid) for pid in pids.read_text().split()]
    assert len(leftover) == 2
    assert not any(_alive(pid) for pid in leftover)


def _fake_rg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Put a fake ``rg`` first on PATH."""
    bin_directory = tmp_path / "rg-bin"
    bin_directory.mkdir()
    fake = bin_directory / "rg"
    fake.write_text("#!/bin/sh\n" + body)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")


_RG_MATCH = (
    '{"type":"match","data":{"path":{"text":"./a.txt"},"lines":{"text":"needle\\n"},'
    '"line_number":1,"submatches":[{"start":0,"end":6}]}}'
)


async def test_a_slow_reader_does_not_use_up_the_search_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The time limit bounds ripgrep, not how long the client takes to read the matches."""
    monkeypatch.setattr("omnigent.host.workspace_search.SCRIPT_TIMEOUT_S", 2)
    matches = f"while [ $i -lt $n ]; do printf '%s\\n' '{_RG_MATCH}'; i=$((i+1)); done\n"
    # The last matches come after the reader has held two batches past the limit.
    _fake_rg(tmp_path, monkeypatch, f"i=0; n=200; {matches}sleep 2.6; n=250; {matches}")
    root = tmp_path / "workspace"
    root.mkdir()

    responses = []
    async for response in search_text(root, SearchTextRequest(query="needle")):
        responses.append(response)
        await asyncio.sleep(1.2)

    assert [len(r.matches) for r in responses] == [100, 100, 50]
    assert responses[-1].done


@needs_rg
async def test_an_rg_planted_in_the_workspace_never_runs_from_a_relative_path_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "planted-ran"
    (tmp_path / "rg").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (tmp_path / "rg").chmod(0o755)
    (tmp_path / "a.txt").write_text("needle\n")
    monkeypatch.setenv("PATH", f".{os.pathsep}{os.environ['PATH']}")

    responses = await _search_text(tmp_path, "needle")

    assert _lines(responses) == ["a.txt:1"]
    assert not marker.exists()


@needs_rg
async def test_an_excluded_directory_ending_in_a_space_is_the_one_excluded(
    tmp_path: Path,
) -> None:
    """Ripgrep reads globs as ignore lines, which drop an unescaped trailing space."""
    for directory in ("build ", "build"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "hit.txt").write_text("needle\n")

    responses = await _search_text(tmp_path, "needle", exclude_directories=["build "])

    assert _lines(responses) == ["build/hit.txt:1"]


async def test_files_staged_since_the_untracked_list_are_listed_and_ranked_once(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    for index in range(40):
        (root / f"note{index:02}.md").write_text("x")
    await _fuzzy(root, "")  # remembers the 40 untracked notes
    _git(root, "add", ".")  # now tracked too, while the untracked list is reused

    listed = await _fuzzy(root, "", maximum_results=81)
    ranked = await _fuzzy(root, "note", maximum_results=40)

    assert len(_paths(listed)) == len(set(_paths(listed))) == 41  # a.txt and the notes
    assert len(set(_paths(ranked))) == 40


async def test_tracked_and_untracked_files_beneath_a_folder_are_compared_by_full_path(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")  # tracks a.txt
    (root / "src").mkdir()
    (root / "src" / "a.txt").write_text("x")
    (root / "src" / "b.txt").write_text("x")
    await _fuzzy(root, "", path="src")  # remembers both as untracked
    _git(root, "add", "src/b.txt")  # now tracked too, while the untracked list is reused

    ranked = await _fuzzy(root, "a", path="src")
    listed = await _fuzzy(root, "", path="src")

    assert _paths(ranked) == ["a.txt"]  # not hidden by the tracked a.txt above src
    assert _paths(listed) == ["a.txt", "b.txt"]  # b.txt once


async def test_fuzzy_mode_keeps_ranking_past_many_deleted_files(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    for index in range(40):
        (root / f"report{index:02}.md").write_text("x")
    (root / "docs").mkdir()
    (root / "docs" / "quarterly_report_draft.md").write_text("x")
    _git(root, "add", ".")
    for index in range(40):
        (root / f"report{index:02}.md").unlink()  # still in the index, gone from disk

    response = await _fuzzy(root, "report", maximum_results=5)

    assert _paths(response) == ["docs/quarterly_report_draft.md"]
    assert not response.truncated


async def test_a_match_past_a_full_shortlist_is_still_found_behind_deleted_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A shortlist no bigger than the limit, as for a limit of 2,000 or more.
    monkeypatch.setattr(workspace_fuzzy, "_POOL", 2)
    monkeypatch.setattr(workspace_search, "_SPARE_RANKED", 1)
    root = _repository(tmp_path / "repo")
    for index in range(6):
        (root / "src" / f"d{index}").mkdir(parents=True)
        (root / "src" / f"d{index}" / "report.md").write_text("x")
    _git(root, "add", ".")
    for index in range(5):
        (root / "src" / f"d{index}" / "report.md").unlink()  # still in the index

    # The query spans folders, so file names alone never match it.
    response = await _fuzzy(root, "src report", maximum_results=1)

    assert _paths(response) == ["src/d5/report.md"]
    assert not response.truncated


@pytest.mark.parametrize(("kept", "truncated"), [((0,), False), ((0, 33), True)])
async def test_a_filled_limit_says_more_matched_only_for_a_file_still_there(
    tmp_path: Path, kept: tuple[int, ...], truncated: bool
) -> None:
    root = _repository(tmp_path / "repo")
    for index in range(34):
        (root / f"report{index:02}.md").write_text("x")
    _git(root, "add", ".")
    for index in set(range(34)) - set(kept):
        (root / f"report{index:02}.md").unlink()  # still in the index, gone from disk

    response = await _fuzzy(root, "report", maximum_results=1)

    assert _paths(response) == ["report00.md"]
    assert response.truncated is truncated


async def test_a_walk_that_runs_out_of_budget_says_the_list_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_search, "_SEARCH_SCAN_BUDGET", 10)
    for index in range(30):
        (tmp_path / f"file{index:02}.txt").write_text("x")

    response = await _fuzzy(tmp_path, "")

    assert response.index_incomplete
