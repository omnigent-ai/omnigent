"""Serialize CockroachDB schema-change DDL across xdist workers.

The migration tests here run Alembic upgrades/downgrades, and each schema
change commits under SERIALIZABLE isolation. CockroachDB's descriptor and
system tables are cluster-wide — shared even across the per-worker test
databases (``omnigent_test_w0``, ``omnigent_test_w1``, …) — so DDL from
parallel workers contends on them and CockroachDB aborts a transaction with
``RETRY_SERIALIZABLE``. The tests invoke migrations directly, bypassing the
product's serialization-retry path, so a losing worker surfaces the abort as
a hard failure — a different migration test each run.

Serialize the DDL-heavy tests behind a cross-process lock so only one worker
mutates the shared schema catalog at a time. Scoped to CockroachDB (other
dialects keep per-database isolation) and to the migration modules (the small
non-DDL ``tests/db`` unit tests stay fully parallel).
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# One lock file per runner; all workers on the host contend on it.
_CRDB_SCHEMA_LOCK = Path(tempfile.gettempdir()) / "omnigent-crdb-schema-ddl.lock"


def _runs_schema_ddl(node: pytest.Item) -> bool:
    """Whether a test issues migration DDL (the modules that hit the catalog)."""
    name = node.path.name
    return name.startswith("test_migration") or name == "test_cockroachdb.py"


@pytest.fixture(autouse=True)
def _serialize_crdb_schema_ddl(
    request: pytest.FixtureRequest, _worker_db_uri: str
) -> Iterator[None]:
    if "cockroachdb" not in _worker_db_uri or not _runs_schema_ddl(request.node):
        yield
        return
    from filelock import FileLock

    with FileLock(str(_CRDB_SCHEMA_LOCK)):
        yield
