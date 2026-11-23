"""Ranking file paths for SearchFiles' fuzzy mode."""

from __future__ import annotations

import functools
import threading
import time

import pytest

from omnigent.host import workspace_fuzzy
from omnigent.host.workspace_fuzzy import CorpusCache, PathCorpus, rank, utf16_indices


def _ranked(paths: list[str], query: str, limit: int = 10) -> list[str]:
    hits, _ = rank([PathCorpus(paths)], query, limit)
    return [hit.path for hit in hits]


def test_a_name_match_outranks_one_spread_over_folders() -> None:
    paths = [
        "web/src/shell/fast/track.ts",
        "workspace/settings/fast/tools.ts",
        "web/electron/workspaceFastTrack.ts",
    ]

    assert _ranked(paths, "wsft")[0] == "web/electron/workspaceFastTrack.ts"


def test_every_word_must_match_and_case_matters_only_for_capitals() -> None:
    paths = ["src/Main.py", "src/main.rs", "docs/maintenance.md"]

    assert _ranked(paths, "main py") == ["src/Main.py"]
    assert set(_ranked(paths, "main")) == {"src/Main.py", "src/main.rs", "docs/maintenance.md"}
    assert _ranked(paths, "Main") == ["src/Main.py"]


def test_a_typo_matches_nothing() -> None:
    paths = ["src/main.py"]

    # A swapped, wrong or extra letter isn't forgiven.
    assert _ranked(paths, "mian") == []
    assert _ranked(paths, "maxn") == []
    assert _ranked(paths, "mainx") == []


def test_ties_go_to_the_shorter_path_then_path_order() -> None:
    paths = ["b/deep/x/notes.md", "b/notes.md", "a/notes.md"]

    assert _ranked(paths, "notes.md") == ["a/notes.md", "b/notes.md", "b/deep/x/notes.md"]


def test_the_limit_caps_results_and_says_more_matched() -> None:
    paths = [f"src/file{index}.ts" for index in range(30)]

    hits, more = rank([PathCorpus(paths)], "file", 5)
    every, rest = rank([PathCorpus(paths)], "file", 30)

    assert len(hits) == 5 and more
    assert len(every) == 30 and not rest


def test_the_best_match_is_found_behind_many_weaker_ones() -> None:
    # Weak matches, by path and by name, come first in list order, past the shortlist's size.
    decoys = [f"q/a_x_b_x_c_x_d_x_e_{index}.txt" for index in range(5000)]

    assert _ranked([*decoys, "abcde.py"], "abcde", 1) == ["abcde.py"]


def test_corpora_are_ranked_together() -> None:
    tracked = PathCorpus(["lib/shared/helpers.py"])
    untracked = PathCorpus(["helpers.py"])

    hits, _ = rank([tracked, untracked], "helpers", 10)

    assert [hit.path for hit in hits] == ["helpers.py", "lib/shared/helpers.py"]


@pytest.mark.parametrize("query", ["", "   ", "zzzz"])
def test_blank_or_unmatched_queries_find_nothing(query: str) -> None:
    assert rank([PathCorpus(["a.txt"])], query, 10) == ([], False)


def test_highlights_count_utf16_code_units_in_the_name_when_it_matches() -> None:
    # "é" is one code unit, "😀" two; the highlights sit on "foo" in the name.
    hits, _ = rank([PathCorpus(["é😀/foo.py", "fxoxo/bar.py"])], "foo", 10)

    assert hits[0].path == "é😀/foo.py"
    assert hits[0].matched_indices == [4, 5, 6]


def test_matched_non_ascii_characters_are_highlighted_at_their_utf16_index() -> None:
    corpus = PathCorpus(["src/été.md", "src/a😀b.txt"])

    accented, _ = rank([corpus], "été", 10)
    emoji, _ = rank([corpus], "😀b", 10)

    # The matcher reports UTF-8 offsets; "é" is one code unit, "😀" two, starting at 5.
    assert [(hit.path, hit.matched_indices) for hit in accented] == [("src/été.md", [4, 5, 6])]
    assert [(hit.path, hit.matched_indices) for hit in emoji] == [("src/a😀b.txt", [5, 7])]


def test_highlights_fall_back_to_the_path_when_the_name_alone_doesnt_match() -> None:
    hits, _ = rank([PathCorpus(["src/lib/app.ts"])], "srclib", 10)

    assert hits[0].matched_indices == [0, 1, 2, 4, 5, 6]


def test_utf8_offsets_become_utf16_indices() -> None:
    text = "aé😀b"  # UTF-8 offsets 0, 1, 3, 7

    assert utf16_indices(text, [0, 1, 3, 7]) == [0, 1, 2, 4]
    assert utf16_indices(text, [7], start=10) == [14]


def test_a_cached_corpus_is_reused_until_one_of_its_source_lists_changes() -> None:
    cache = CorpusCache()
    built: list[list[str]] = []

    def paths(source: list[str]) -> list[str]:
        built.append(source)
        return list(source)

    tracked, untracked = ["a.py"], ["b.py"]
    one = cache.corpus("key", (tracked, untracked), functools.partial(paths, untracked))
    again = cache.corpus("key", (tracked, untracked), functools.partial(paths, untracked))
    # The untracked files' corpus depends on the tracked list too.
    restaged = ["a.py", "b.py"]
    changed = cache.corpus("key", (restaged, untracked), functools.partial(paths, untracked))

    assert one is again and len(built) == 2
    assert changed is not one


def test_more_is_reported_when_the_limit_fills_the_shortlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_fuzzy, "_POOL", 2)  # below the limit, as for a large one

    hits, more = rank([PathCorpus([f"src/file{index}.py" for index in range(5)])], "file", 3)

    assert len(hits) == 3 and more


def test_a_corpus_being_built_holds_up_only_searches_for_the_same_key() -> None:
    cache = CorpusCache()
    ready_source, slow_source = ["a.py"], ["b.py"]
    ready = cache.corpus("ready", (ready_source,), lambda: list(ready_source))
    started, release = threading.Event(), threading.Event()
    builds: list[str] = []

    def slow_paths() -> list[str]:
        builds.append("slow")
        started.set()
        release.wait(10)
        return list(slow_source)

    found: dict[str, object] = {}

    def look_up(name: str, key: str, source: list[str]) -> None:
        found[name] = cache.corpus(key, (source,), slow_paths)

    first = threading.Thread(target=look_up, args=("first", "slow", slow_source))
    first.start()
    assert started.wait(10)
    second = threading.Thread(target=look_up, args=("second", "slow", slow_source))
    other = threading.Thread(target=look_up, args=("other", "ready", ready_source))
    second.start()
    other.start()
    other.join(timeout=2)
    other_waited = other.is_alive()
    release.set()
    for thread in (first, second, other):
        thread.join(10)

    assert not other_waited  # a ready corpus is returned during another key's build
    assert found["other"] is ready
    assert found["first"] is found["second"] and builds == ["slow"]  # built once


def test_a_key_is_built_once_at_a_time_even_as_its_sources_change() -> None:
    cache = CorpusCache()
    old_source, new_source = ["a.py"], ["a.py", "b.py"]
    started = {name: threading.Event() for name in ("old", "new", "later")}
    release = {name: threading.Event() for name in ("old", "new", "later")}
    found: dict[str, object] = {}

    def look_up(name: str, source: list[str]) -> None:
        def paths() -> list[str]:
            started[name].set()
            release[name].wait(10)
            return list(source)

        found[name] = cache.corpus("key", (source,), paths)

    threads = {
        name: threading.Thread(target=look_up, args=(name, source))
        for name, source in (("old", old_source), ("new", new_source), ("later", new_source))
    }
    threads["old"].start()
    assert started["old"].wait(10)
    threads["new"].start()  # waits for the old build, then rebuilds for the new source
    # Nothing signals that it is waiting for the lock, so it gets a moment to reach it.
    time.sleep(0.2)  # custom-lint: disable=sleep-then-assert -- no signal for a blocked lock
    release["old"].set()
    assert started["new"].wait(10)
    threads["later"].start()  # arrives during the rebuild
    later_built = started["later"].wait(0.5)
    for event in release.values():
        event.set()
    for thread in threads.values():
        thread.join(10)

    assert not later_built  # it waited for the rebuild instead of building alongside it
    assert found["later"] is found["new"]


def test_older_corpora_are_dropped_past_the_path_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_fuzzy, "_MAX_CACHED_CORPUS_PATHS", 3)
    cache = CorpusCache()
    older, newer = ["a.py", "b.py"], ["c.py", "d.py"]
    first = cache.corpus("older", (older,), lambda: list(older))
    cache.corpus("newer", (newer,), lambda: list(newer))

    rebuilt = cache.corpus("older", (older,), lambda: list(older))

    # Each corpus holds 2 paths and keeps its 2-path source: 8 in all, past 3, so "older" went.
    assert rebuilt is not first


def test_the_source_lists_corpora_keep_count_toward_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_fuzzy, "_MAX_CACHED_CORPUS_PATHS", 4)
    separate = CorpusCache()
    first_workspace, second_workspace = ["a/x.py", "b/y.py"], ["c/x.py", "d/y.py"]
    first = separate.corpus("first", (first_workspace,), lambda: ["x.py"])
    separate.corpus("second", (second_workspace,), lambda: ["x.py"])
    shared = CorpusCache()
    workspace = ["a/x.py", "b/y.py"]
    scoped = shared.corpus("a", (workspace,), lambda: ["x.py"])
    shared.corpus("b", (workspace,), lambda: ["y.py"])

    # Two one-path corpora keep 6 paths alive through their sources: over the budget.
    assert separate.corpus("first", (first_workspace,), lambda: ["x.py"]) is not first
    # A source both corpora keep counts once: 4 paths.
    assert shared.corpus("a", (workspace,), lambda: ["x.py"]) is scoped


def test_without_its_library_the_host_offers_no_fuzzy_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_fuzzy, "_frizbee", lambda: None)

    assert workspace_fuzzy.features() == ()
    assert rank([], "x", 10) == ([], False)
    with pytest.raises(RuntimeError):
        PathCorpus(["a.txt"])
