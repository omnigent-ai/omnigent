"""Per-agent ToolState for stateful ``@tool`` functions.

See ``designs/TOOL_STATE.md`` for the full design. The primitive is
a simple key-value store, JSON-serialized, scoped to one
(conversation, agent) pair via the storage directory provided by
the framework. The ``@tool`` decorator hides ``ToolState``-typed
parameters from the LLM-facing schema; the subprocess runner
reconstructs a ``ToolState`` from the directory path and injects it
when the tool function is called.

Tool authors see::

    from omnigent_client import tool, ToolState

    @tool
    def add_task(desc: str, state: ToolState) -> str:
        with state.transaction("queue") as q:
            q = q or []
            q.append({"desc": desc})
            return f"#{len(q) - 1}"

and nothing else.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on native Windows
    _fcntl = None  # type: ignore[assignment]

if os.name == "nt":
    import msvcrt as _msvcrt
else:  # pragma: no cover - exercised on POSIX
    _msvcrt = None  # type: ignore[assignment]

# Subdirectory segments reserved by the framework — JSON key files
# live at ``{root}/{key}.json``. Keys must not contain path
# separators; we sanitize eagerly rather than allowing the bug to
# surface as directory traversal.
_KEY_SUFFIX = ".json"
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[Path, threading.Lock] = {}


class ToolState:
    """Per-agent, per-conversation key-value state for ``@tool`` functions.

    Values are JSON-serialized. The keyspace is shared across every
    tool invoked for the same registered agent within the same
    conversation. Use :meth:`transaction` for atomic read-modify-write;
    plain :meth:`get` and :meth:`set` do not serialize concurrent
    writers on the same key.

    Instances are constructed by the framework. Tool authors receive
    a ``ToolState`` by declaring a parameter of this type on their
    ``@tool``-decorated function; the decorator strips the parameter
    from the LLM-facing schema and the subprocess runner injects the
    live ``ToolState`` at call time.

    :param root: The directory this namespace lives in, e.g.
        ``{workspace}/.tool_state/{agent_id}``. The directory does
        not need to exist yet; it is created lazily on first write.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    # ── Primary API ──────────────────────────────────────────

    def get(self, key: str, *, default: Any = None) -> Any:
        """Return the stored value at ``key``, or ``default`` if absent.

        :param key: The state key, e.g. ``"queue"``.
        :param default: Value to return when the key has never been
            written. ``None`` by default.
        :returns: The deserialized JSON value, or ``default``.
        """
        path = self._path_for(key)
        if not path.exists():
            return default
        with path.open("r") as f:
            # Shared lock: allow parallel reads, block concurrent writers
            # briefly so we see a complete JSON payload.
            _lock_file(f, exclusive=False)
            try:
                return json.loads(f.read() or "null")
            finally:
                _unlock_file(f)

    def set(self, key: str, value: Any) -> None:
        """Replace (or create) the value at ``key``. JSON-serialized.

        Non-atomic relative to concurrent writers on the same key —
        use :meth:`transaction` for read-modify-write sequences.

        :param key: The state key.
        :param value: Any JSON-serializable value.
        """
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write through a temp file + rename so a reader never sees
        # a half-written JSON payload even without a lock.
        tmp = path.with_suffix(_KEY_SUFFIX + ".tmp")
        with tmp.open("w") as f:
            json.dump(value, f)
        tmp.replace(path)

    def delete(self, key: str) -> None:
        """Remove ``key``. No-op if absent.

        :param key: The state key to remove.
        """
        path = self._path_for(key)
        # Idempotent delete — tools commonly don't know whether the
        # key was ever set.
        with suppress(FileNotFoundError):
            path.unlink()

    def keys(self) -> list[str]:
        """List all keys currently stored in this namespace.

        :returns: Sorted list of keys, e.g. ``["counter", "queue"]``.
            Empty list if nothing has been written yet.
        """
        if not self._root.exists():
            return []
        return sorted(p.stem for p in self._root.iterdir() if p.suffix == _KEY_SUFFIX)

    def __contains__(self, key: object) -> bool:
        """Return whether ``key`` currently exists in this namespace.

        :param key: Candidate key, e.g. ``"queue"``.
        :returns: ``True`` when ``key`` is a valid stored key, else ``False``.
        """
        if not isinstance(key, str):
            return False
        return self._path_for(key).exists()

    @contextmanager
    def transaction(self, key: str, *, default: Any = None) -> Iterator[Any]:
        """Atomic read-modify-write for one key.

        Typical usage — supply a ``default`` so first-time callers
        get a usable container without a ``None`` check::

            with state.transaction("queue", default=[]) as queue:
                queue.append(item)
            # queue is written back on normal exit.

        The yielded value is the current contents, or a fresh
        ``default`` if the key was never set. Mutating the yielded
        object in place is the expected pattern — the same object
        is serialized back on exit. Rebinding the local name inside
        the ``with`` block does NOT propagate (Python closures), so
        for "replace the value" semantics use :meth:`set` explicitly.

        On a normal exit the yielded object is JSON-serialized and
        written back. On exception no write happens — the prior
        value is preserved.

        :param key: The state key to lock + read + write.
        :param default: Value yielded when the key has never been
            written. Defaults to ``None``. Pass ``[]`` or ``{}``
            (or any JSON-serializable value) to skip the absent-key
            branch in caller code.
        :yields: The current value at ``key``, or ``default`` if
            the key has no stored value yet. Mutate in place.
        """
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        thread_lock = _get_thread_lock(path)
        # ``a+`` creates the file if missing and positions at end;
        # we seek to 0 before read. Opening with ``r+`` would fail
        # when the file doesn't exist yet, which is a common first-
        # call case for a tool.
        with thread_lock, path.open("a+") as f:
            _lock_file(f, exclusive=True)
            try:
                value = _read_transaction_value(f, default)
                yield value
                _write_transaction_value(f, value)
            finally:
                _unlock_file(f)

    # ── Internals ────────────────────────────────────────────

    def _path_for(self, key: str) -> Path:
        """Resolve ``key`` to the on-disk path, rejecting traversal.

        :param key: Caller-supplied key.
        :returns: The ``{root}/{key}.json`` path.
        :raises ValueError: If ``key`` is empty, contains a path
            separator, or starts with a dot (no hidden or
            escaped paths).
        """
        if not key:
            raise ValueError("ToolState key must be a non-empty string")
        if "/" in key or "\\" in key or key.startswith("."):
            # Rejects traversal and hidden-file sigils. Authors who
            # really need slashes can encode them (e.g. "a__b") —
            # we'd rather break loudly than accept quiet bugs.
            raise ValueError(
                f"ToolState key {key!r} contains an illegal character. "
                f"Keys must be plain names (no '/', '\\', or leading '.')."
            )
        return self._root / f"{key}{_KEY_SUFFIX}"


def _read_transaction_value(f: Any, default: Any) -> Any:
    """Seek to 0 and decode the JSON value under the open file handle.

    Returns ``default`` when the file is empty (first-time use of
    the key). Factored out of :meth:`ToolState.transaction` so the
    context manager stays short.

    :param f: Open file handle positioned anywhere; will be seek(0)ed.
    :param default: Value to return on empty/whitespace content.
    :returns: Decoded JSON value or ``default``.
    """
    f.seek(0)
    raw = f.read()
    if raw.strip():
        return json.loads(raw)
    return default


def _write_transaction_value(f: Any, value: Any) -> None:
    """Truncate and re-serialize ``value`` as JSON under the file handle.

    Caller must hold the exclusive flock before calling. Factored
    out of :meth:`ToolState.transaction` so the context manager
    stays short.

    :param f: Open file handle (must support ``r+``-style truncate).
    :param value: Any JSON-serializable value to persist.
    """
    f.seek(0)
    f.truncate()
    json.dump(value, f)
    # Flush before releasing the flock held by the caller. Python file objects
    # buffer writes; if we unlock before flushing, another process can acquire
    # the lock and read stale on-disk contents, losing the prior update.
    f.flush()
    os.fsync(f.fileno())


def _lock_file(f: Any, *, exclusive: bool) -> None:
    """Acquire an advisory file lock for ``f``.

    POSIX uses ``fcntl.flock``. Native Windows has no ``fcntl`` module, so it
    locks one byte with ``msvcrt.locking``; that API is exclusive-only, which
    is conservative for reads but preserves cross-process serialization.
    """
    if _fcntl is not None:
        _fcntl.flock(f, _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH)
        return
    if _msvcrt is None:  # pragma: no cover - defensive for unusual platforms
        return
    f.seek(0)
    _msvcrt.locking(f.fileno(), _msvcrt.LK_LOCK, 1)


def _unlock_file(f: Any) -> None:
    """Release a lock acquired by :func:`_lock_file`."""
    if _fcntl is not None:
        _fcntl.flock(f, _fcntl.LOCK_UN)
        return
    if _msvcrt is None:  # pragma: no cover - defensive for unusual platforms
        return
    f.seek(0)
    _msvcrt.locking(f.fileno(), _msvcrt.LK_UNLCK, 1)


def _get_thread_lock(path: Path) -> threading.Lock:
    """Return the per-key in-process mutex for ``path``.

    ``flock`` serializes across processes, but threads in the same
    process can still interleave on separate file descriptors. This
    helper layers a per-path ``threading.Lock`` on top so
    ``transaction()`` is atomic under both thread and process
    contention.

    :param path: The key file path, e.g. ``Path("/tmp/state/queue.json")``.
    :returns: The shared mutex guarding that path within this process.
    """
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[path] = lock
        return lock
