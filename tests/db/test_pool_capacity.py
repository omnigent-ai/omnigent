"""Tests for the startup database-pool capacity report.

Each replica keeps its own connection pool open, so the budget a deployment
consumes is (replicas x per-replica ceiling). The report exists so an
operator sizing a rollout learns that from a startup log line instead of
from ``FATAL: too many connections`` on every replica at once.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine

from omnigent.db.utils import (
    configured_max_overflow,
    configured_pool_size,
    pool_capacity_report,
    report_pool_capacity,
)


def test_room_for_several_replicas_is_reported_as_a_count() -> None:
    """A generous limit reports how many replicas fit."""
    capacity = pool_capacity_report(50, 400)

    assert capacity.replicas == 8
    assert capacity.scales_out is True
    assert "up to 8 replicas fit" in capacity.message


def test_single_replica_budget_names_the_knobs_to_lower() -> None:
    """A budget that fits exactly one replica must not read as healthy.

    This is the shape that breaks a scale-out: the first replica works, so
    nothing looks wrong until a second one starts and every connection
    attempt fails.
    """
    capacity = pool_capacity_report(220, 300)

    assert capacity.replicas == 1
    assert capacity.scales_out is False
    assert "exactly one replica fits" in capacity.message
    assert "OMNIGENT_DB_POOL_SIZE" in capacity.message
    assert "OMNIGENT_DB_MAX_OVERFLOW" in capacity.message


def test_ceiling_above_the_limit_warns_about_a_single_replica() -> None:
    """A pool larger than the database's limit can't even run one replica."""
    capacity = pool_capacity_report(220, 100)

    assert capacity.replicas == 0
    assert capacity.scales_out is False
    assert "even one replica can exhaust it" in capacity.message


def test_unlimited_overflow_is_reported_as_unbounded() -> None:
    """``max_overflow=-1`` has no ceiling to compare, so it warns on its own."""
    capacity = pool_capacity_report(None, 100)

    assert capacity.replicas is None
    assert capacity.scales_out is False
    assert "unlimited" in capacity.message


def test_defaults_match_the_documented_pool_size() -> None:
    """The report reads the same knobs the engine is built with."""
    assert configured_pool_size() == 200
    assert configured_max_overflow() == 20


def test_env_overrides_are_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator overrides flow into the reported ceiling."""
    monkeypatch.setenv("OMNIGENT_DB_POOL_SIZE", "30")
    monkeypatch.setenv("OMNIGENT_DB_MAX_OVERFLOW", "5")

    assert configured_pool_size() == 30
    assert configured_max_overflow() == 5


def test_sqlite_engines_are_not_reported(
    tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    """SQLite locks a file rather than pooling connections, so it's skipped.

    Uses a real (throwaway) SQLite engine registered in the process-wide
    engine cache the reporter walks, so the dialect skip is exercised
    end-to-end rather than through the pure helper.
    """
    from omnigent.db import utils

    engine = create_engine("sqlite://")
    with caplog.at_level(logging.INFO, logger="omnigent.db.utils"):
        with utils._engine_lock:
            utils._engine_cache["sqlite://:capacity-test"] = engine
        try:
            reported = report_pool_capacity()
        finally:
            with utils._engine_lock:
                utils._engine_cache.pop("sqlite://:capacity-test", None)

    assert reported == []
    assert "connections per replica" not in caplog.text
