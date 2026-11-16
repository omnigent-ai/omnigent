"""One cross-process lock for CockroachDB schema changes in a test run."""

from __future__ import annotations

import functools
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from filelock import FileLock

# One lock file per machine; every xdist worker contends on it.
_CRDB_SCHEMA_LOCK = Path(tempfile.gettempdir()) / "omnigent-crdb-schema-ddl.lock"


@functools.cache
def _process_lock(path: str) -> FileLock:
    # One instance per process, so nested holders re-enter instead of deadlocking.
    from filelock import FileLock

    return FileLock(path)


@contextmanager
def crdb_schema_ddl_lock(dialect: str, *, wait_s: float | None = None) -> Iterator[bool]:
    """Hold the lock while issuing CockroachDB DDL; other dialects run unlocked.

    CockroachDB's descriptor and system tables are cluster-wide, so concurrent
    DDL from another worker's database can abort a schema change with
    ``RETRY_SERIALIZABLE``. Re-entrant in one process: a migration test holds it
    while its setup creates the worker database, which takes it again.

    :param wait_s: Longest wait for the lock, e.g. ``120.0``; ``None`` waits forever.
    :returns: Whether the lock is held; ``False`` only when *wait_s* ran out.
    """
    if dialect != "cockroachdb":
        yield True
        return
    from filelock import Timeout

    lock = _process_lock(str(_CRDB_SCHEMA_LOCK))
    try:
        lock.acquire(timeout=-1 if wait_s is None else wait_s)
    except Timeout:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()
