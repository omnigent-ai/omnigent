"""Workspace-namespaced in-process caches.

One OSS server process serves many workspaces: multi-tenant deployments
bind :func:`~omnigent.db.db_models.current_workspace_id` per request (OSS
leaves it at the default ``0``). A module-level cache keyed by a
workspace-collidable id therefore leaks across tenants on a shared pod —
``conversation_id`` / ``session_id`` collide across workspaces (imported
sessions), and one ``user_id`` / email can belong to several workspaces —
so a request in workspace B can read the value workspace A cached.

:class:`WorkspaceScopedCache` and :class:`WorkspaceScopedSet` wrap a
backing mapping / set and namespace every entry by the active workspace:
the caller passes the bare id, the wrapper stores it under
``(current_workspace_id(), key)``. Per-key operations and iteration see
only the current workspace's slice, so call sites stay unchanged while
cross-tenant reads become impossible. :meth:`WorkspaceScopedCache.clear`
is the one exception — a global reset primitive that wipes every
workspace, matching its only callers (process-wide store re-wiring and
"invalidate everything" seams).

The ``dev/lint/lint_workspace_scoped_cache.py`` hook requires every
module-level cache-like global under ``omnigent/server`` and
``omnigent/runtime`` to be one of these types, unless it is allow-listed
there because its key is already globally unique (``call_id``,
``runner_id``, ``elicitation_id``, …) or already contains the workspace id.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping
from typing import Any, Generic, TypeVar, overload

from omnigent.db.db_models import current_workspace_id

K = TypeVar("K")
V = TypeVar("V")
D = TypeVar("D")

# Sentinel distinguishing "no default supplied" from an explicit ``None``
# default in :meth:`WorkspaceScopedCache.pop`.
_MISSING: object = object()


class WorkspaceScopedCache(Generic[K, V]):
    """A dict-like cache whose keys are transparently namespaced by workspace.

    Construct with a *factory* producing the backing
    ``MutableMapping[tuple[int, K], V]`` — ``dict`` (default) for an
    unbounded cache, or e.g. ``lambda: cachetools.LRUCache(maxsize=4096)``
    for a bounded one (eviction then bounds the combined cross-workspace
    keyspace, exactly as a raw tuple-keyed cache would).

    Every per-key operation and all iteration are scoped to
    ``current_workspace_id()``. Iteration returns list snapshots so callers
    can mutate during a sweep.
    """

    def __init__(self, factory: Callable[[], MutableMapping[tuple[int, K], V]] = dict) -> None:
        self._backing: MutableMapping[tuple[int, K], V] = factory()

    def _scoped(self, key: K) -> tuple[int, K]:
        return (current_workspace_id(), key)

    def __getitem__(self, key: K) -> V:
        return self._backing[self._scoped(key)]

    def __setitem__(self, key: K, value: V) -> None:
        self._backing[self._scoped(key)] = value

    def __delitem__(self, key: K) -> None:
        del self._backing[self._scoped(key)]

    def __contains__(self, key: object) -> bool:
        return (current_workspace_id(), key) in self._backing

    def get(self, key: K, default: V | None = None) -> V | None:
        return self._backing.get(self._scoped(key), default)

    def setdefault(self, key: K, default: V) -> V:
        return self._backing.setdefault(self._scoped(key), default)

    @overload
    def pop(self, key: K) -> V: ...
    @overload
    def pop(self, key: K, default: D) -> V | D: ...
    def pop(self, key: K, default: Any = _MISSING) -> Any:
        scoped = self._scoped(key)
        if default is _MISSING:
            return self._backing.pop(scoped)
        return self._backing.pop(scoped, default)

    def keys(self) -> list[K]:
        """Current workspace's keys, in backing order (a snapshot)."""
        ws = current_workspace_id()
        return [key for (w, key) in self._backing if w == ws]

    def values(self) -> list[V]:
        """Current workspace's values (a snapshot)."""
        ws = current_workspace_id()
        return [value for (w, _key), value in self._backing.items() if w == ws]

    def items(self) -> list[tuple[K, V]]:
        """Current workspace's ``(key, value)`` pairs (a snapshot)."""
        ws = current_workspace_id()
        return [(key, value) for (w, key), value in self._backing.items() if w == ws]

    def __iter__(self) -> Iterator[K]:
        return iter(self.keys())

    def __len__(self) -> int:
        """Number of entries in the current workspace only."""
        ws = current_workspace_id()
        return sum(1 for (w, _key) in self._backing if w == ws)

    def clear(self) -> None:
        """Drop EVERY workspace's entries (process-wide reset primitive)."""
        self._backing.clear()

    def all_values(self) -> list[V]:
        """Every workspace's values — the cross-workspace escape hatch.

        For the rare global sweep that must run context-free (e.g. the
        server-shutdown broadcast in :mod:`omnigent.runtime.session_stream`,
        which signals all subscribers regardless of workspace). Ordinary
        callers use :meth:`values`.
        """
        return list(self._backing.values())


class WorkspaceScopedSet(Generic[K]):
    """A set whose members are transparently namespaced by workspace.

    Membership, iteration, and length are scoped to
    ``current_workspace_id()``; :meth:`clear` wipes every workspace.
    """

    def __init__(self) -> None:
        self._backing: set[tuple[int, K]] = set()

    def add(self, key: K) -> None:
        self._backing.add((current_workspace_id(), key))

    def discard(self, key: K) -> None:
        self._backing.discard((current_workspace_id(), key))

    def __contains__(self, key: object) -> bool:
        return (current_workspace_id(), key) in self._backing

    def __iter__(self) -> Iterator[K]:
        ws = current_workspace_id()
        return iter([key for (w, key) in self._backing if w == ws])

    def __len__(self) -> int:
        ws = current_workspace_id()
        return sum(1 for (w, _key) in self._backing if w == ws)

    def clear(self) -> None:
        """Drop EVERY workspace's members (process-wide reset primitive)."""
        self._backing.clear()


__all__ = ["WorkspaceScopedCache", "WorkspaceScopedSet"]
