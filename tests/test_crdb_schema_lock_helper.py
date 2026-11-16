"""Tests for the shared CockroachDB schema-change lock."""

from __future__ import annotations

import threading

from tests._helpers.crdb_schema_lock import crdb_schema_ddl_lock


def test_nested_holders_in_one_process_re_enter_the_lock() -> None:
    # A migration test holds the lock while its setup creates the worker database.
    entered = threading.Event()

    def nested() -> None:
        with crdb_schema_ddl_lock("cockroachdb"), crdb_schema_ddl_lock("cockroachdb"):
            entered.set()

    worker = threading.Thread(target=nested, daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert entered.is_set(), "a nested holder blocked on its own lock"
