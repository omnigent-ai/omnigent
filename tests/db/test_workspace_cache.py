"""Tests for :mod:`omnigent.db.workspace_cache`.

The wrapper's whole point is that the SAME bare key stored under two
different ``workspace_scope`` contexts never collides, so a request in one
workspace can't read another's cached value.
"""

from __future__ import annotations

import threading
import time
from types import MappingProxyType
from typing import Any

import cachetools
import pytest

from omnigent.db.db_models import DEFAULT_WORKSPACE_ID, workspace_scope
from omnigent.db.workspace_cache import WorkspaceScopedCache, WorkspaceScopedSet


def test_same_key_isolated_across_workspaces() -> None:
    """A bare key set in workspace 1 is invisible in workspace 2."""
    cache: WorkspaceScopedCache[str, str] = WorkspaceScopedCache()
    with workspace_scope(1):
        cache["conv_shared"] = "ws1-value"
    with workspace_scope(2):
        cache["conv_shared"] = "ws2-value"
        assert cache["conv_shared"] == "ws2-value"
        assert cache.get("conv_shared") == "ws2-value"
    with workspace_scope(1):
        assert cache["conv_shared"] == "ws1-value"
        assert "conv_shared" in cache


def test_get_and_contains_scoped() -> None:
    """``get``/``in`` only see the current workspace."""
    cache: WorkspaceScopedCache[str, int] = WorkspaceScopedCache()
    with workspace_scope(1):
        cache["k"] = 7
    with workspace_scope(2):
        assert cache.get("k") is None
        assert cache.get("k", -1) == -1
        assert "k" not in cache


def test_setdefault_returns_mutable_inner_for_nested_dicts() -> None:
    """``setdefault`` returns the live inner value for nested-dict caches."""
    cache: WorkspaceScopedCache[str, dict[str, int]] = WorkspaceScopedCache()
    with workspace_scope(1):
        cache.setdefault("user", {})["conv"] = 100
        assert cache["user"] == {"conv": 100}
    with workspace_scope(2):
        # Same outer key, different workspace — starts empty.
        assert cache.setdefault("user", {}) == {}


def test_update_is_workspace_scoped() -> None:
    """``update`` merges into the current workspace only."""
    cache: WorkspaceScopedCache[str, int] = WorkspaceScopedCache()
    with workspace_scope(1):
        cache.update({"a": 1, "b": 2})
        assert sorted(cache.items()) == [("a", 1), ("b", 2)]
    with workspace_scope(2):
        assert cache.get("a") is None
        cache.update({"a": 9})
        assert cache["a"] == 9
    with workspace_scope(1):
        assert cache["a"] == 1


def test_update_from_own_mapping_proxy_completes() -> None:
    cache: WorkspaceScopedCache[str, int] = WorkspaceScopedCache()
    cache.update({"a": 1, "b": 2})
    errors: list[Exception] = []

    def update_from_proxy() -> None:
        try:
            cache.update(MappingProxyType(cache))
        except Exception as error:
            errors.append(error)

    # A regression must fail within the timeout, not leave a blocking worker.
    worker = threading.Thread(target=update_from_proxy, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "update deadlocked while reading its own mapping proxy"
    assert errors == []
    assert cache.items() == [("a", 1), ("b", 2)]


def test_update_source_error_preserves_destination() -> None:
    class FailingSource(dict[str, int]):
        def items(self) -> Any:
            yield "new", 2
            raise RuntimeError("source failed")

    cache: WorkspaceScopedCache[str, int] = WorkspaceScopedCache(reset_on_error=True)
    cache["existing"] = 1

    with pytest.raises(RuntimeError, match="source failed"):
        cache.update(FailingSource())

    assert cache.items() == [("existing", 1)]


def test_pop_scoped_and_default() -> None:
    """``pop`` is workspace-scoped and honors the default sentinel."""
    cache: WorkspaceScopedCache[str, str] = WorkspaceScopedCache()
    with workspace_scope(1):
        cache["k"] = "v"
    with workspace_scope(2):
        # pop() mutates, so run it as a statement, not inside assert (which -O strips).
        other_ws = cache.pop("k", None)
        assert other_ws is None
    with workspace_scope(1):
        popped = cache.pop("k")
        assert popped == "v"
        missing = cache.pop("k", "fallback")
        assert missing == "fallback"


def test_iteration_is_workspace_scoped() -> None:
    """``keys``/``values``/``items``/``len`` only report the current workspace."""
    cache: WorkspaceScopedCache[str, int] = WorkspaceScopedCache()
    with workspace_scope(1):
        cache["a"] = 1
        cache["b"] = 2
    with workspace_scope(2):
        cache["c"] = 3
        assert sorted(cache.keys()) == ["c"]
        assert cache.values() == [3]
        assert cache.items() == [("c", 3)]
        assert len(cache) == 1
        assert list(cache) == ["c"]
    with workspace_scope(1):
        assert sorted(cache.keys()) == ["a", "b"]
        assert len(cache) == 2


def test_all_values_crosses_workspaces() -> None:
    """``all_values`` is the escape hatch that ignores workspace scope."""
    cache: WorkspaceScopedCache[str, int] = WorkspaceScopedCache()
    with workspace_scope(1):
        cache["a"] = 1
    with workspace_scope(2):
        cache["b"] = 2
    # Even under a third (empty) workspace, all_values sees everything.
    with workspace_scope(3):
        assert sorted(cache.all_values()) == [1, 2]
        assert cache.values() == []


def test_clear_is_global() -> None:
    """``clear`` wipes every workspace, matching its reset-primitive callers."""
    cache: WorkspaceScopedCache[str, int] = WorkspaceScopedCache()
    with workspace_scope(1):
        cache["a"] = 1
    with workspace_scope(2):
        cache["b"] = 2
        cache.clear()
    with workspace_scope(1):
        assert "a" not in cache


def test_bounded_backing_evicts_across_combined_keyspace() -> None:
    """A cachetools factory bounds the combined cross-workspace keyspace."""
    cache: WorkspaceScopedCache[str, int] = WorkspaceScopedCache(
        lambda: cachetools.LRUCache(maxsize=2)
    )
    with workspace_scope(1):
        cache["a"] = 1
    with workspace_scope(2):
        cache["b"] = 2
        cache["c"] = 3  # evicts the LRU entry (ws1's "a") from the shared backing
    with workspace_scope(1):
        assert cache.get("a") is None


def test_set_isolated_across_workspaces() -> None:
    """``WorkspaceScopedSet`` membership is per-workspace."""
    fenced: WorkspaceScopedSet[str] = WorkspaceScopedSet()
    with workspace_scope(1):
        fenced.add("conv")
        assert "conv" in fenced
    with workspace_scope(2):
        assert "conv" not in fenced
        fenced.add("conv")
        assert list(fenced) == ["conv"]
        assert len(fenced) == 1
    with workspace_scope(1):
        fenced.discard("conv")
        assert "conv" not in fenced


def test_default_workspace_is_isolated_from_others() -> None:
    """OSS default workspace (0) is just another isolated slice."""
    cache: WorkspaceScopedCache[str, str] = WorkspaceScopedCache()
    cache["k"] = "default"  # no scope → DEFAULT_WORKSPACE_ID
    with workspace_scope(DEFAULT_WORKSPACE_ID):
        assert cache["k"] == "default"
    with workspace_scope(5):
        assert cache.get("k") is None


class _OverlapProbeTTLCache(cachetools.TTLCache[Any, Any]):
    """TTLCache that records when two threads are inside it at once.

    Each entry point yields the GIL before delegating, so unserialized
    callers reliably interleave instead of depending on scheduler luck.
    """

    def __init__(self) -> None:
        super().__init__(maxsize=64, ttl=0.001)
        self._probe_guard = threading.Lock()
        self._inside: dict[int, int] = {}
        self.overlaps = 0

    def _enter(self) -> None:
        me = threading.get_ident()
        with self._probe_guard:
            if any(tid != me for tid in self._inside):
                self.overlaps += 1
            self._inside[me] = self._inside.get(me, 0) + 1
        time.sleep(0)

    def _exit(self) -> None:
        me = threading.get_ident()
        with self._probe_guard:
            self._inside[me] -= 1
            if not self._inside[me]:
                del self._inside[me]

    def __getitem__(self, key: Any) -> Any:
        self._enter()
        try:
            return super().__getitem__(key)
        finally:
            self._exit()

    def __setitem__(self, key: Any, value: Any) -> None:
        self._enter()
        try:
            super().__setitem__(key, value)
        finally:
            self._exit()

    def __delitem__(self, key: Any) -> None:
        self._enter()
        try:
            super().__delitem__(key)
        finally:
            self._exit()

    def expire(self, time: Any = None) -> Any:
        self._enter()
        try:
            return super().expire(time)
        finally:
            self._exit()


def test_concurrent_access_to_ttl_backing_is_serialized() -> None:
    """Threads hammering get/set/pop never enter the TTL backing together.

    Unserialized access corrupts cachetools' expiry links, after which every
    write raises ``KeyError`` until the process restarts.
    """
    probe = _OverlapProbeTTLCache()
    cache: WorkspaceScopedCache[str, int] = WorkspaceScopedCache(lambda: probe)
    errors: list[Exception] = []
    start = threading.Barrier(8)

    def hammer(worker: int) -> None:
        start.wait()
        try:
            for iteration in range(300):
                key = f"conv_{iteration % 5}"
                cache[key] = worker
                cache.get(key)
                cache.pop(key, None)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(worker,), daemon=True) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert probe.overlaps == 0


class _CorruptedTTLCache(cachetools.TTLCache[Any, Any]):
    """TTLCache whose expiry links are broken, as after unlocked writes."""

    def __init__(self) -> None:
        super().__init__(maxsize=64, ttl=30)

    def expire(self, time: Any = None) -> Any:
        raise KeyError((0, "conv_corrupted"))


def test_reset_on_error_replaces_corrupted_backing() -> None:
    """A raising backing degrades to a miss and is swapped for a fresh one."""
    cache: WorkspaceScopedCache[str, int] = WorkspaceScopedCache(
        lambda: cachetools.TTLCache(maxsize=64, ttl=30), reset_on_error=True
    )
    cache._backing = _CorruptedTTLCache()

    cache["k"] = 1  # dropped: the corrupted backing is replaced instead
    assert cache.get("k") is None
    cache["k"] = 2
    assert cache.get("k") == 2


def test_backing_errors_propagate_without_reset_on_error() -> None:
    """Stateful (non-cache) instances keep failing loudly by default."""
    cache: WorkspaceScopedCache[str, int] = WorkspaceScopedCache(_CorruptedTTLCache)
    with pytest.raises(KeyError):
        cache["k"] = 1


class _RaisingMapping(dict[Any, Any]):
    """Backing mapping whose reads and writes raise a non-``KeyError`` failure."""

    def __contains__(self, key: object) -> bool:
        raise RuntimeError("corrupted backing")

    def get(self, key: Any, default: Any = None) -> Any:
        raise RuntimeError("corrupted backing")

    def pop(self, key: Any, default: Any = None) -> Any:
        raise RuntimeError("corrupted backing")

    def setdefault(self, key: Any, default: Any = None) -> Any:
        raise RuntimeError("corrupted backing")

    def __setitem__(self, key: Any, value: Any) -> None:
        raise RuntimeError("corrupted backing")


_FALLIBLE_OPERATIONS: dict[str, tuple[Any, Any]] = {
    "get": (lambda cache: cache.get("k", "miss"), "miss"),
    "contains": (lambda cache: "k" in cache, False),
    "pop": (lambda cache: cache.pop("k", "miss"), "miss"),
    "setdefault": (lambda cache: cache.setdefault("k", 7), 7),
    "update": (lambda cache: cache.update({"k": 7}), None),
}


@pytest.mark.parametrize("operation", sorted(_FALLIBLE_OPERATIONS))
def test_reset_on_error_recovers_each_operation(
    operation: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Each guarded operation degrades to a miss, logs, and swaps the backing."""
    call, expected = _FALLIBLE_OPERATIONS[operation]
    cache: WorkspaceScopedCache[str, Any] = WorkspaceScopedCache(reset_on_error=True)
    corrupted = _RaisingMapping()
    cache._backing = corrupted

    with caplog.at_level("WARNING", logger="omnigent.db.workspace_cache"):
        assert call(cache) == expected

    assert f"after {operation} failed" in caplog.text
    assert cache._backing is not corrupted
    cache["k"] = 1
    assert cache.get("k") == 1


@pytest.mark.parametrize("operation", sorted(_FALLIBLE_OPERATIONS))
def test_operation_errors_propagate_without_reset_on_error(operation: str) -> None:
    """Without the opt-in, backing failures surface and the backing is kept."""
    call, _expected = _FALLIBLE_OPERATIONS[operation]
    cache: WorkspaceScopedCache[str, Any] = WorkspaceScopedCache()
    corrupted = _RaisingMapping()
    cache._backing = corrupted

    with pytest.raises(RuntimeError, match="corrupted backing"):
        call(cache)
    assert cache._backing is corrupted
