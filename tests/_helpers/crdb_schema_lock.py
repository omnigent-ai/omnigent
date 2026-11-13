"""One cross-process lock for CockroachDB schema changes in a test run."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# One lock file per machine; every xdist worker contends on it.
_CRDB_SCHEMA_LOCK = Path(tempfile.gettempdir()) / "omnigent-crdb-schema-ddl.lock"


@contextmanager
def crdb_schema_ddl_lock(dialect: str) -> Iterator[None]:
    """Hold the lock while issuing CockroachDB DDL; other dialects run unlocked.

    CockroachDB's descriptor and system tables are cluster-wide, so concurrent
    DDL from another worker's database can abort a schema change with
    ``RETRY_SERIALIZABLE``.
    """
    if dialect != "cockroachdb":
        yield
        return
    from filelock import FileLock

    with FileLock(str(_CRDB_SCHEMA_LOCK)):
        yield
