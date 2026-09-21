"""Exercise the documented cohort joins against synthetic lifecycle logs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


class _CountIf:
    def __init__(self) -> None:
        self.count = 0

    def step(self, value: object) -> None:
        self.count += bool(value)

    def finalize(self) -> int:
        return self.count


def _query(rows: list[tuple[object, ...]]) -> dict[str, object]:
    sql = (Path(__file__).parents[1] / "designs/session_creation_success.sql").read_text()
    # Only adapt dialect syntax; keep the production joins and aggregation intact.
    sql = sql.replace("IDENTIFIER(:debug_logs_table)", "logs_input").replace("GREATEST(", "MAX(")
    for name in ("cohort_start", "cohort_end", "rollout_at", "as_of"):
        sql = sql.replace(f"CAST(:{name} AS TIMESTAMP)", f":{name}")
    sql = sql.replace("INTERVAL 7 MINUTES", "420").replace("INTERVAL 5 MINUTES", "300")
    for field in ("request_id", "runner_id", "creation_kind"):
        sql = sql.replace(f"attributes['{field}']", f"json_extract(attributes, '$.{field}')")
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.create_aggregate("COUNT_IF", 1, _CountIf)
        db.execute(
            "CREATE TABLE logs_input (client_time REAL, source TEXT, event_name TEXT, "
            "session_id TEXT, attributes TEXT, workspace_id TEXT)"
        )
        db.executemany("INSERT INTO logs_input VALUES (?, ?, ?, ?, ?, ?)", rows)
        row = db.execute(
            sql,
            {
                "cohort_start": 0,
                "cohort_end": 3000,
                "rollout_at": 0,
                "as_of": 3000,
                "workspace_id": "0",
            },
        ).fetchone()
        return dict(row)


def _event(
    name: str,
    timestamp: int,
    *,
    request: str | None = None,
    session: str | None = None,
    runner: str | None = None,
    kind: str = "top_level",
) -> tuple[object, ...]:
    return (
        timestamp,
        "server",
        name,
        session,
        json.dumps({"request_id": request, "runner_id": runner, "creation_kind": kind}),
        "0",
    )


def _create(
    request: str, start: int = 1000, *, kind: str = "top_level"
) -> list[tuple[object, ...]]:
    return [
        _event("session_creation_started", start, request=request),
        _event(
            "session_created",
            start + 1,
            request=request,
            session=request,
            runner="runner",
            kind=kind,
        ),
    ]


def test_query_counts_requests_not_spawns_errors_or_reconnects() -> None:
    rows = _create("success") + _create("late") + _create("wrong-runner")
    rows += _create("child", kind="child") + _create("recent", start=2900)
    rows += [
        _event("session_creation_started", 1000, request="rejected", kind="unknown"),
        _event("session_creation_failed", 1001, request="rejected", kind="unknown"),
        _event("runner_launch_failed", 1002, session="success", runner="runner"),
        _event("session_runner_ready", 1020, session="success", runner="runner"),
        _event("session_runner_ready", 1040, session="success", runner="runner"),
        _event("session_runner_ready", 1301, session="late", runner="runner"),
        _event("session_runner_ready", 1020, session="wrong-runner", runner="unrelated"),
        _event("session_runner_ready", 1020, session="existing-session", runner="runner"),
    ]
    result = _query(rows)
    assert result == {
        "creation_count": 4,
        "successful_creations": 1,
        "failed_creations": 3,
        "unmeasurable_creations": 0,
        "creation_success_rate_pct": 25.0,
    }


@pytest.mark.parametrize("replacement_ready", [False, True])
def test_query_ignores_readiness_outside_binding_lifetime(replacement_ready: bool) -> None:
    rows = [
        *_create("replace"),
        _event("session_runner_bound", 1010, session="replace", runner="new-runner"),
        _event("session_runner_ready", 1020, session="replace", runner="runner"),
    ]
    if replacement_ready:
        rows.append(_event("session_runner_ready", 1030, session="replace", runner="new-runner"))
    result = _query(rows)
    assert result["successful_creations"] == int(replacement_ready)
    assert result["creation_count"] == 1


@pytest.mark.parametrize("recovers", [False, True])
def test_query_exposes_measurement_gaps_without_dropping_requests(recovers: bool) -> None:
    rows = [
        *_create("unsupported"),
        _event("session_readiness_unavailable", 1010, session="unsupported", runner="runner"),
    ]
    if recovers:
        rows.append(_event("session_runner_ready", 1030, session="unsupported", runner="runner"))
    result = _query(rows)
    assert result["creation_count"] == 1
    assert result["creation_success_rate_pct"] == (100 if recovers else None)
    assert result["unmeasurable_creations"] == (0 if recovers else 1)
