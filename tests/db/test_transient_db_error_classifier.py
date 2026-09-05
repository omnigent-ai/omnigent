"""Tests for classifying transient store-availability faults.

``is_transient_db_error`` decides whether a database exception is a
retryable availability event (rate limit, suspend/resume, lock, pool
exhaustion) or a genuine defect. The server's exception handlers use it
to answer 503 ``store_unavailable`` instead of the 500
``internal_error`` catch-all, so a misclassification either hides real
bugs (false positive) or pages on-call for routine store hiccups
(false negative).
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from omnigent.db.utils import is_transient_db_error


def _operational(message: str) -> OperationalError:
    """Wrap a driver message the way SQLAlchemy surfaces it."""
    return OperationalError("INSERT INTO t VALUES (1)", {}, sqlite3.OperationalError(message))


@pytest.mark.parametrize(
    "message",
    [
        # SQLite writer contention past busy_timeout — the local analog
        # of a rate-limited hosted store.
        "database is locked",
        "database table is locked",
        # Managed Postgres (e.g. Lakebase) suspending/resuming.
        "the database system is starting up",
        "the database system is shutting down",
        "the database system is in recovery mode",
        "connection refused",
        "server closed the connection unexpectedly",
        "SSL connection has been closed unexpectedly",
        "connection reset by peer",
        "connection timed out",
        # Connection/rate limiting.
        "too many connections",
        "FATAL: too many clients already",
        "remaining connection slots are reserved",
        "rate limit exceeded",
    ],
)
def test_transient_availability_faults_are_retryable(message: str) -> None:
    """Every known availability-fault shape classifies as transient."""
    assert is_transient_db_error(_operational(message))


def test_matching_is_case_insensitive() -> None:
    """Driver casing varies (e.g. 'Connection refused'); match loosely."""
    assert is_transient_db_error(_operational("Database Is Locked"))


@pytest.mark.parametrize(
    "message",
    [
        # Genuine defects that also arrive as OperationalError.
        "no such table: conversations",
        "no such column: q.bogus",
        'syntax error at or near "SELEC"',
        "attempt to write a readonly database",
    ],
)
def test_defect_shaped_operational_errors_stay_internal(message: str) -> None:
    """An OperationalError that signals a bug must NOT look retryable."""
    assert not is_transient_db_error(_operational(message))


def test_pool_checkout_timeout_is_transient() -> None:
    """Pool exhaustion drains as in-flight work completes — retryable."""
    assert is_transient_db_error(
        SqlAlchemyTimeoutError("QueuePool limit of size 5 overflow 10 reached")
    )


def test_invalidated_connection_is_transient() -> None:
    """A dialect-flagged dead connection is replaced by the pool — retryable."""
    exc = DBAPIError("SELECT 1", {}, sqlite3.Error("gone"), connection_invalidated=True)
    assert is_transient_db_error(exc)


def test_integrity_error_is_not_transient() -> None:
    """Constraint violations are data/logic errors, never availability."""
    exc = IntegrityError(
        "INSERT INTO t VALUES (1)",
        {},
        sqlite3.IntegrityError("UNIQUE constraint failed: t.id"),
    )
    assert not is_transient_db_error(exc)


def test_plain_exception_is_not_transient() -> None:
    """Non-database exceptions never classify as store availability."""
    assert not is_transient_db_error(ValueError("database is locked"))
