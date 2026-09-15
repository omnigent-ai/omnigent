"""Tests for :mod:`omnigent.db.workspace_cache`.

The wrapper's whole point is that the SAME bare key stored under two
different ``workspace_scope`` contexts never collides, so a request in one
workspace can't read another's cached value.
"""

from __future__ import annotations

import cachetools

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
