"""A short-lived cache of a project's Summary, KPI and drill-down inputs.

The Summary page, its KPI tiles and their drill-downs read the same expensive
inputs: spend, merged PRs and ticket stage history. Each is cached per
(workspace, project, input, arguments) so one page load, a window switch or a
reopen computes it once. Permission checks stay per request, outside the cache.

Writes that change a project's numbers clear its entries once their transaction
commits (:func:`invalidate_after_commit`); a 60-second TTL is the safety net
for anything not hooked. A generation counter per project keeps a computation
that raced an invalidation from storing pre-write results.

The cache is per process: a server running several workers keeps one
short-lived copy in each, which is fine at the TTL above.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any, ClassVar, Literal, TypeVar

from sqlalchemy import event
from sqlalchemy.orm import Session

from omnigent.db.db_models import current_workspace_id

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: What an entry was computed from, so a write clears only what it changes.
StatusInput = Literal["spend", "prs", "tickets"]
STATUS_INPUTS: tuple[StatusInput, ...] = ("spend", "prs", "tickets")

_TTL_S = 60.0
_MAX_ENTRIES = 512
_PENDING_KEY = "project_status_cache_pending"

_Key = tuple[int, str, StatusInput, Hashable]

#: Called as ``listener(project_id, inputs)`` after each invalidation, e.g. to
#: refresh derived rows. Runs in the invalidating thread, outside the cache lock.
InvalidationListener = Callable[[str, tuple[StatusInput, ...]], None]
_listeners: list[InvalidationListener] = []
_listeners_lock = threading.Lock()


def add_invalidation_listener(callback: InvalidationListener) -> None:
    """Have *callback* hear every project invalidation; adding it twice is a no-op."""
    with _listeners_lock:
        if callback not in _listeners:
            _listeners.append(callback)


def remove_invalidation_listener(callback: InvalidationListener) -> None:
    with _listeners_lock:
        if callback in _listeners:
            _listeners.remove(callback)


class ProjectStatusCache:
    """An LRU of computed inputs with single-flight misses and per-project generations."""

    def __init__(self, *, ttl_s: float = _TTL_S, max_entries: int = _MAX_ENTRIES) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[_Key, tuple[float, Any]] = OrderedDict()
        self._inflight: dict[_Key, threading.Event] = {}
        # (workspace, project, input) -> generation; bumped by every invalidation.
        self._generations: dict[tuple[int, str, StatusInput], int] = {}

    def get_or_compute(
        self, project_id: str, status_input: StatusInput, args: Hashable, compute: Callable[[], T]
    ) -> T:
        """The cached value for *args*, computing it once when missing or expired.

        Concurrent misses for the same key wait for the first caller's result.
        A failed computation isn't cached; waiters then compute for themselves.
        """
        workspace_id = current_workspace_id()
        key: _Key = (workspace_id, project_id, status_input, args)
        generation_key = (workspace_id, project_id, status_input)
        while True:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and time.monotonic() - entry[0] < self._ttl_s:
                    self._entries.move_to_end(key)
                    return entry[1]
                waiting = self._inflight.get(key)
                if waiting is None:
                    done = threading.Event()
                    self._inflight[key] = done
                    generation = self._generations.get(generation_key, 0)
                    break
            waiting.wait()
        try:
            value = compute()
        except BaseException:
            with self._lock:
                self._inflight.pop(key, None)
            done.set()
            raise
        with self._lock:
            self._inflight.pop(key, None)
            # A write committed while computing: the value may predate it.
            if self._generations.get(generation_key, 0) == generation:
                self._entries[key] = (time.monotonic(), value)
                self._entries.move_to_end(key)
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
        done.set()
        return value

    def has_entries(self) -> bool:
        """Whether anything is cached, so hot write paths can skip resolving a project."""
        return bool(self._entries) or bool(self._inflight)

    def invalidate(self, project_id: str, inputs: tuple[StatusInput, ...] = STATUS_INPUTS) -> None:
        """Drop the project's cached *inputs* and stop in-flight ones from being stored."""
        workspace_id = current_workspace_id()
        with self._lock:
            for status_input in inputs:
                generation_key = (workspace_id, project_id, status_input)
                self._generations[generation_key] = self._generations.get(generation_key, 0) + 1
            for key in [
                key
                for key in self._entries
                if key[0] == workspace_id and key[1] == project_id and key[2] in inputs
            ]:
                del self._entries[key]
        with _listeners_lock:
            listeners = list(_listeners)
        for listener in listeners:
            try:
                listener(project_id, inputs)
            except Exception:
                logger.warning("A status cache listener failed for %s", project_id, exc_info=True)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._generations.clear()


#: The process's cache; stores clear it on writes, the status routes read through it.
project_status_cache = ProjectStatusCache()


def invalidate_after_commit(
    session: Session, project_id: str | None, inputs: tuple[StatusInput, ...] = STATUS_INPUTS
) -> None:
    """Clear *project_id*'s *inputs* once *session*'s transaction commits.

    Clearing after the commit, not before, keeps a concurrent reader from
    caching the numbers as they were just before the write.
    """
    if not project_id:
        return
    pending: dict[str, set[StatusInput]] | None = session.info.get(_PENDING_KEY)
    if pending is None:
        pending = {}
        session.info[_PENDING_KEY] = pending
        event.listen(session, "after_commit", _flush_pending)
        event.listen(session, "after_rollback", _drop_pending)
    pending.setdefault(str(project_id), set()).update(inputs)


def _flush_pending(session: Session) -> None:
    pending: dict[str, set[StatusInput]] = session.info.get(_PENDING_KEY) or {}
    for project_id, inputs in list(pending.items()):
        try:
            project_status_cache.invalidate(project_id, tuple(inputs))
        except Exception:
            logger.warning("Couldn't clear the status cache for %s", project_id, exc_info=True)
    pending.clear()


def _drop_pending(session: Session) -> None:
    pending = session.info.get(_PENDING_KEY)
    if pending:
        pending.clear()


def period_key(period: Any) -> tuple[str, str, str | None]:
    """A status period's cache key: its window and days, not the request's second."""
    return (period.window, period.today_utc, period.since_day_utc)


class CachedSource:
    """A spend or PRs-merged source whose reads go through :data:`project_status_cache`.

    Only the methods the wrapped source has are exposed, so ``hasattr`` checks
    on optional methods (``merged_items``, ``root_days``) still work.
    """

    # Method -> (cached input, how its non-project arguments form the key).
    _METHODS: ClassVar[dict[str, tuple[StatusInput, Callable[..., Hashable]]]] = {
        "spend": ("spend", period_key),
        "root_days": ("spend", period_key),
        "prs_merged": ("prs", period_key),
        "merged_items": ("prs", lambda since_epoch=None: since_epoch),
    }

    def __init__(self, source: Any, cache: ProjectStatusCache) -> None:
        self._source = source
        self._cache = cache

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._source, name)
        if name not in self._METHODS:
            return method
        status_input, key_of = self._METHODS[name]

        def cached(project: Any, *args: Any) -> Any:
            return self._cache.get_or_compute(
                project.id, status_input, (name, key_of(*args)), lambda: method(project, *args)
            )

        return cached


def cached_source(source: Any, cache: ProjectStatusCache | None = None) -> Any:
    """Wrap *source* so its reads are cached; ``None`` and wrapped sources pass through."""
    if source is None or isinstance(source, CachedSource):
        return source
    return CachedSource(source, cache or project_status_cache)
