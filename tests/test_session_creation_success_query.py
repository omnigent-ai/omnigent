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
    for field in (
        "request_id",
        "runner_id",
        "creation_kind",
        "harness",
        "terminal_name",
        "superseded",
    ):
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
    harness: str = "claude-native",
    source: str | None = None,
    **attrs: object,
) -> tuple[object, ...]:
    if source is None:
        source = (
            "runner"
            if name.startswith(("native_", "terminal_")) or name == "runner_session_initialized"
            else "server"
        )
    return (
        timestamp,
        source,
        name,
        session,
        json.dumps(
            {
                "request_id": request,
                "runner_id": runner,
                "creation_kind": kind,
                "harness": harness,
                **attrs,
            }
        ),
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


def _ready(
    session: str, at: int = 1020, *, runner: str = "runner", harness: str = "claude-native"
) -> list[tuple[object, ...]]:
    return [
        _event("runner_connected", at - 10, runner=runner),
        _event(
            "runner_session_initialized", at - 5, session=session, runner=runner, harness=harness
        ),
        _event("runner_stream_ready", at - 2, session=session, runner=runner),
        _event("native_input_ready", at, session=session, runner=runner, harness=harness),
    ]


def test_query_counts_requests_not_spawns_errors_or_reconnects() -> None:
    rows = _create("success") + _create("late") + _create("wrong-runner")
    rows += _create("child", kind="child") + _create("recent", start=2900)
    rows += [
        _event("session_creation_started", 1000, request="rejected", kind="unknown"),
        _event("session_creation_failed", 1001, request="rejected", kind="unknown"),
        _event("runner_launch_failed", 1002, session="success", runner="runner"),
    ]
    rows += _ready("success") + _ready("success") + _ready("late", 1301)
    rows += _ready("wrong-runner", runner="unrelated") + _ready("existing-session")
    assert _query(rows) == {
        "creation_count": 4,
        "successful_creations": 1,
        "failed_creations": 3,
        "unmeasurable_creations": 0,
        "creation_success_rate_pct": 25.0,
    }


@pytest.mark.parametrize("replacement_ready", [False, True])
def test_query_ignores_readiness_outside_binding_lifetime(replacement_ready: bool) -> None:
    rows = _create("replace") + _ready("replace")
    rows.append(_event("session_runner_bound", 1010, session="replace", runner="new-runner"))
    if replacement_ready:
        rows += _ready("replace", 1030, runner="new-runner")
    assert _query(rows)["successful_creations"] == int(replacement_ready)


@pytest.mark.parametrize("recovers", [False, True])
def test_query_exposes_measurement_gaps_without_dropping_requests(recovers: bool) -> None:
    rows = _create("unsupported") + _ready("unsupported", harness="pi-native")
    if recovers:
        rows += _ready("unsupported", 1050, harness="codex-native")
    result = _query(rows)
    assert result["creation_count"] == 1
    assert result["creation_success_rate_pct"] == (100 if recovers else None)
    assert result["unmeasurable_creations"] == (0 if recovers else 1)


@pytest.mark.parametrize(
    "missing",
    [
        "runner_connected",
        "runner_session_initialized",
        "runner_stream_ready",
        "native_input_ready",
    ],
)
def test_each_required_signal_is_necessary(missing: str) -> None:
    rows = _create("session") + [r for r in _ready("session") if r[2] != missing]
    assert _query(rows)["successful_creations"] == 0


@pytest.mark.parametrize("reinitialized", [False, True])
def test_native_ready_survives_reconnect_but_init_and_relay_must_be_current(
    reinitialized: bool,
) -> None:
    rows = _create("session") + _ready("session")
    # First connection dies before native input becomes ready: no overlap.
    rows += [
        _event("runner_disconnected", 1019, runner="runner"),
        _event("runner_connected", 1025, runner="runner"),
        _event("runner_stream_ready", 1028, session="session", runner="runner"),
    ]
    if reinitialized:
        rows.append(_event("runner_session_initialized", 1027, session="session", runner="runner"))
    assert _query(rows)["successful_creations"] == int(reinitialized)


@pytest.mark.parametrize(
    "boundary",
    [
        "native_input_starting",
        "native_input_stopped",
        "terminal_exit_observed",
        "terminal_close_requested",
        "runner_stream_closed",
    ],
)
@pytest.mark.parametrize("recovers", [False, True])
def test_stale_native_or_relay_readiness_cannot_complete_creation(
    boundary: str, recovers: bool
) -> None:
    rows = _create("session") + [
        r for r in _ready("session") if r[2] != "runner_session_initialized"
    ]
    rows += [
        _event(boundary, 1021, session="session", runner="runner", terminal_name="claude"),
        _event("runner_session_initialized", 1025, session="session", runner="runner"),
    ]
    if recovers:
        event = (
            "runner_stream_ready" if boundary == "runner_stream_closed" else "native_input_ready"
        )
        rows.append(_event(event, 1030, session="session", runner="runner"))
    assert _query(rows)["successful_creations"] == int(recovers)


def test_shared_runner_and_repeated_binding() -> None:
    rows = _create("session") + [
        r
        for r in _ready("session")
        if r[2] not in ("runner_connected", "runner_session_initialized")
    ]
    rows += [
        _event("runner_connected", 100, runner="runner"),
        _event("session_runner_bound", 1021, session="session", runner="runner"),
        _event("runner_session_initialized", 1025, session="session", runner="runner"),
    ]
    assert _query(rows)["successful_creations"] == 1


def test_no_start_event_means_no_denominator_or_rate() -> None:
    result = _query(_ready("invisible"))
    assert result["creation_count"] == 0
    assert result["creation_success_rate_pct"] is None


def test_unbind_ends_the_old_binding() -> None:
    rows = _create("session") + _ready("session")
    rows.append(_event("session_runner_unbound", 1019, session="session", runner="runner"))
    assert _query(rows)["successful_creations"] == 0
