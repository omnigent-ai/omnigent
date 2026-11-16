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
def _process_lock() -> FileLock:
    # One instance per process, so nested holders re-enter instead of deadlocking.
    from filelock import FileLock

    return FileLock(str(_CRDB_SCHEMA_LOCK))


@contextmanager
def crdb_schema_ddl_lock(dialect: str) -> Iterator[None]:
    """Hold the lock while issuing CockroachDB DDL; other dialects run unlocked.

    CockroachDB's descriptor and system tables are cluster-wide, so concurrent
    DDL from another worker's database can abort a schema change with
    ``RETRY_SERIALIZABLE``. Re-entrant in one process: a migration test holds it
    while its setup creates the worker database, which takes it again.
    """
    if dialect != "cockroachdb":
        yield
        return
    with _process_lock():
        yield
