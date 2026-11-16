"""Tests for the shared CockroachDB schema-change lock."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tests._helpers import crdb_schema_lock
from tests._helpers.crdb_schema_lock import crdb_schema_ddl_lock


def test_nested_holders_in_one_process_re_enter_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A migration test holds the lock while its setup creates the worker database.
    monkeypatch.setattr(crdb_schema_lock, "_CRDB_SCHEMA_LOCK", tmp_path / "schema.lock")
    entered = threading.Event()

    def nested() -> None:
        with crdb_schema_ddl_lock("cockroachdb"), crdb_schema_ddl_lock("cockroachdb"):
            entered.set()

    worker = threading.Thread(target=nested, daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert entered.is_set(), "a nested holder blocked on its own lock"
