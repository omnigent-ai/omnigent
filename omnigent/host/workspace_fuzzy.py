"""Ranked fuzzy matching of file paths, for SearchFiles' fuzzy mode.

A path scores its match plus its file name's, so a query the name matches on its
own outranks one spread over directories. Each whitespace-separated word must
match. Matching runs in frizbee; a host without it doesn't offer fuzzy mode.
"""

from __future__ import annotations

import functools
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from frizbee import Haystacks, Match, Matcher

# Candidates kept by path and by name before the exact rerank.
_POOL = 2000
# Below this many paths, one thread matches faster than starting more.
_PARALLEL_FROM = 50_000
# Matching 1.6 million paths on 32 cores: 9-12 ms at 8 threads, 7-8 ms at 16, none faster past it.
_MAX_THREADS = 16
_MAX_CACHED_CORPORA = 8
# Paths kept in cached corpora; the oldest go first, keeping the newest.
_MAX_CACHED_CORPUS_PATHS = 4_000_000


@functools.cache
def _frizbee() -> ModuleType | None:
    try:
        import frizbee
    except ImportError:  # no wheel for this platform
        return None
    return frizbee


def available() -> bool:
    """Whether this host can rank paths."""
    return _frizbee() is not None


def features() -> tuple[str, ...]:
    """The SearchFiles features fuzzy ranking provides, when it's available."""
    return ("search_files.fuzzy", "search_files.browse") if available() else ()


def file_name(path: str) -> str:
    """The last component of a ``/``-separated path."""
    return path.rsplit("/", 1)[-1]


class PathCorpus:
    """Paths copied to the matcher once, with their file names, for many queries.

    :param paths: Paths relative to the directory searched.
    :raises RuntimeError: When fuzzy matching is unavailable.
    """

    def __init__(self, paths: Sequence[str]) -> None:
        frizbee = _frizbee()
        if frizbee is None:
            raise RuntimeError("Fuzzy matching is unavailable on this host")
        self.paths = paths if isinstance(paths, list) else list(paths)
        # Names are matched in frizbee and derived again only for the shortlisted paths.
        self._path_haystacks: Haystacks = frizbee.Haystacks(self.paths)
        self._name_haystacks: Haystacks = frizbee.Haystacks(map(file_name, self.paths))

    def __len__(self) -> int:
        return len(self.paths)

    def match(self, matcher: Matcher, *, names: bool) -> list[Match]:
        """Match every path, or every file name, releasing the GIL meanwhile."""
        haystacks = self._name_haystacks if names else self._path_haystacks
        if len(self.paths) < _PARALLEL_FROM:
            return matcher.match_list(haystacks)
        threads = min(_MAX_THREADS, max(1, (os.cpu_count() or 2) - 1))  # leave one core
        return matcher.match_list_parallel(haystacks, threads)


class CorpusCache:
    """Recently built corpora, reused while their source lists are the same objects.

    The file lister returns the same list until the repository changes, so a
    query typed into a large repository pays only for the match.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[Hashable, tuple[tuple[Sequence[str], ...], PathCorpus]] = (
            OrderedDict()
        )
        # Guards the entries only: corpora are built outside it, one build per key at a time,
        # so a large repository's build doesn't hold up searches with a corpus ready.
        self._lock = threading.Lock()
        # Each key's build lock, and how many lookups hold or wait for it; dropped after the last.
        self._building: dict[Hashable, tuple[threading.Lock, list[int]]] = {}

    def corpus(
        self,
        key: Hashable,
        sources: tuple[Sequence[str], ...],
        paths: Callable[[], list[str]],
    ) -> PathCorpus:
        """The corpus for ``key``, rebuilt from ``paths()`` when any of ``sources`` changed.

        :param key: What decides the paths besides the sources, e.g. the scope.
        :param sources: The lists the paths are derived from.
        :param paths: Returns the paths, derived from ``sources``.
        """
        with self._lock:
            found = self._current(key, sources)
            if found is not None:
                return found
            building, users = self._building.setdefault(key, (threading.Lock(), [0]))
            users[0] += 1
        try:
            with building:
                with self._lock:
                    found = self._current(key, sources)  # built while this one waited
                if found is not None:
                    return found
                built = PathCorpus(paths())
                with self._lock:
                    self._entries[key] = (sources, built)
                    self._entries.move_to_end(key)
                    while len(self._entries) > 1 and (
                        len(self._entries) > _MAX_CACHED_CORPORA
                        or self._paths_held() > _MAX_CACHED_CORPUS_PATHS
                    ):
                        self._entries.popitem(last=False)
                return built
        finally:
            with self._lock:
                users[0] -= 1
                if not users[0]:
                    del self._building[key]

    def _current(self, key: Hashable, sources: tuple[Sequence[str], ...]) -> PathCorpus | None:
        """The cached corpus for ``key`` if built from these ``sources``; under the lock."""
        cached = self._entries.get(key)
        if (
            cached is None
            or len(cached[0]) != len(sources)
            or any(old is not new for old, new in zip(cached[0], sources, strict=True))
        ):
            return None
        self._entries.move_to_end(key)
        return cached[1]

    def _paths_held(self) -> int:
        """Paths in the lists the entries keep alive, corpora and sources, each list once."""
        held = {
            id(paths): len(paths)
            for sources, corpus in self._entries.values()
            for paths in (*sources, corpus.paths)
        }
        return sum(held.values())


@dataclass(frozen=True)
class RankedPath:
    """One match.

    :param path: As the corpus holds it.
    :param score: Higher is better.
    :param matched_indices: UTF-16 code unit indices of the matched characters, ascending.
    """

    path: str
    score: int
    matched_indices: list[int]


def utf16_indices(text: str, byte_offsets: Sequence[int], *, start: int = 0) -> list[int]:
    """Convert offsets of characters' UTF-8 bytes in ``text`` to UTF-16 indices.

    :param start: Added to each index, for ``text`` that sits inside a longer string.
    """
    wanted = set(byte_offsets)
    indices: list[int] = []
    byte_offset = 0
    utf16 = start
    for char in text:
        if byte_offset in wanted:
            indices.append(utf16)
        byte_offset += len(char.encode())
        utf16 += 2 if ord(char) > 0xFFFF else 1
    return indices


def _highlight(matcher: Matcher, path: str) -> list[int]:
    """The name's matched characters when the name matches alone, else the path's."""
    name = file_name(path)
    hit = matcher.match_one_indices(name, 0)
    if hit is not None:
        folder = path[: len(path) - len(name)]
        start = len(folder.encode("utf-16-le")) // 2
        return sorted(set(utf16_indices(name, hit.indices, start=start)))
    hit = matcher.match_one_indices(path, 0)
    return sorted(set(utf16_indices(path, hit.indices))) if hit is not None else []


def rank(corpora: Sequence[PathCorpus], query: str, limit: int) -> tuple[list[RankedPath], bool]:
    """Rank the paths of ``corpora`` against ``query``, best first.

    Ties go to the shorter path, then path order. Matching ignores case unless
    the query has a capital letter, and allows no typos.

    :param corpora: Disjoint sets of paths, ranked together.
    :param query: Whitespace-separated words; blank matches nothing.
    :param limit: Results to return at most.
    :returns: The best ``limit`` matches, and whether more matched.
    """
    frizbee = _frizbee()
    words = query.split()
    if frizbee is None or not words or limit <= 0:
        return [], False
    # One past the limit, so a full shortlist says more matched.
    shortlist: Matcher = frizbee.Matcher.from_patterns(
        words, casing="smart", max_items=max(_POOL, limit + 1)
    )
    scorer: Matcher = frizbee.Matcher.from_patterns(words, casing="smart")
    scored: list[tuple[int, int, str]] = []
    matched = 0
    for corpus in corpora:
        by_path = corpus.match(shortlist, names=False)
        by_name = corpus.match(shortlist, names=True)
        matched += len(by_path)
        for index in {hit.index for hit in by_path} | {hit.index for hit in by_name}:
            path = corpus.paths[index]
            path_hit = scorer.match_one(path, index)
            if path_hit is None:
                continue
            name_hit = scorer.match_one(file_name(path), index)
            score = path_hit.score + (name_hit.score if name_hit is not None else 0)
            scored.append((-score, len(path), path))
    scored.sort()
    ranked = [
        RankedPath(path=path, score=-negative, matched_indices=_highlight(scorer, path))
        for negative, _, path in scored[:limit]
    ]
    return ranked, matched > limit or len(scored) > limit
