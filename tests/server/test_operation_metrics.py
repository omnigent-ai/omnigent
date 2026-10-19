"""Tests for the Omnigent server's operation metrics."""

from __future__ import annotations

from itertools import chain
from pathlib import Path

import pytest
import tomllib
from fastapi import HTTPException
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    HistogramDataPoint,
    InMemoryMetricReader,
    NumberDataPoint,
)
from packaging.requirements import Requirement

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server import operation_metrics
from omnigent.server.operation_metrics import (
    OperationMetrics,
    classify_outcome,
    scheduled_run_outcome,
    timed_operation,
    track_operation,
    tracked_tunnel,
)


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> InMemoryMetricReader:
    """Route the module's default instruments to an in-memory reader."""
    metric_reader = InMemoryMetricReader()
    metrics = OperationMetrics(MeterProvider(metric_readers=[metric_reader]).get_meter("test"))
    monkeypatch.setattr(operation_metrics, "_default_metrics", lambda: metrics)
    monkeypatch.setattr(operation_metrics, "metrics_enabled", lambda: True)
    return metric_reader


def _points(reader: InMemoryMetricReader, name: str) -> list[tuple[dict[str, object], float]]:
    data = reader.get_metrics_data()
    out: list[tuple[dict[str, object], float]] = []
    for resource_metrics in data.resource_metrics if data else []:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    if isinstance(point, NumberDataPoint):
                        value = point.value
                    else:
                        assert isinstance(point, HistogramDataPoint)
                        value = point.count
                    out.append((dict(point.attributes or {}), value))
    return out


@pytest.mark.parametrize(
    ("status_code", "failed", "error_code", "expected"),
    [
        (201, False, None, "ok"),
        (302, False, None, "ok"),
        (404, False, None, "client_error"),
        (499, False, None, "client_error"),
        (500, False, None, "server_error"),
        (503, False, ErrorCode.RUNNER_UNAVAILABLE, "client_error"),
        (None, True, None, "server_error"),
        (200, True, None, "server_error"),
    ],
)
def test_classify_outcome(
    status_code: int | None, failed: bool, error_code: str | None, expected: str
) -> None:
    assert classify_outcome(status_code, failed=failed, error_code=error_code) == expected


@pytest.mark.parametrize(
    ("status", "error_code", "expected"),
    [
        ("running", None, "ok"),
        ("failed", "launch_failed", "server_error"),
        ("failed", "host_offline", "client_error"),
        ("failed", "owner_deleted", "client_error"),
    ],
)
def test_scheduled_run_outcome(status: str, error_code: str | None, expected: str) -> None:
    assert scheduled_run_outcome(status, error_code) == expected


def test_record_operation_labels_name_and_outcome(reader: InMemoryMetricReader) -> None:
    operation_metrics.record_operation("session_create", 1.5, "ok")

    assert _points(reader, "omnigent.server.operation.duration") == [
        ({"operation.name": "session_create", "outcome": "ok"}, 1)
    ]


def test_recorders_are_noops_when_metrics_disabled(
    reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(operation_metrics, "metrics_enabled", lambda: False)

    operation_metrics.record_operation("session_create", 1.0, "ok")
    operation_metrics.record_error(ErrorCode.INTERNAL_ERROR)

    assert _points(reader, "omnigent.server.operation.duration") == []
    assert _points(reader, "omnigent.server.errors") == []


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (HTTPException(status_code=400), "client_error"),
        (HTTPException(status_code=502), "server_error"),
        (OmnigentError("offline", code=ErrorCode.RUNNER_UNAVAILABLE), "client_error"),
        (RuntimeError("boom"), "server_error"),
    ],
)
def test_track_operation_classifies_exceptions(
    reader: InMemoryMetricReader, exc: Exception, expected: str
) -> None:
    with pytest.raises(type(exc)), track_operation("sandbox_launch"):
        raise exc

    assert _points(reader, "omnigent.server.operation.duration") == [
        ({"operation.name": "sandbox_launch", "outcome": expected}, 1)
    ]


async def test_timed_operation_records_success(reader: InMemoryMetricReader) -> None:
    @timed_operation("sandbox_resume")
    async def resume() -> str:
        return "ok"

    assert await resume() == "ok"
    assert _points(reader, "omnigent.server.operation.duration") == [
        ({"operation.name": "sandbox_resume", "outcome": "ok"}, 1)
    ]


async def test_tracked_tunnel_balances_active_count(reader: InMemoryMetricReader) -> None:
    @tracked_tunnel("runner")
    async def tunnel() -> None:
        assert _points(reader, "omnigent.server.tunnel.active") == [({"tunnel.kind": "runner"}, 1)]
        raise ConnectionError("dropped")

    with pytest.raises(ConnectionError):
        await tunnel()

    assert _points(reader, "omnigent.server.tunnel.active") == [({"tunnel.kind": "runner"}, 0)]
    assert _points(reader, "omnigent.server.tunnel.closed") == [({"tunnel.kind": "runner"}, 1)]


def test_record_error_counts_by_code(reader: InMemoryMetricReader) -> None:
    operation_metrics.record_error(ErrorCode.RUNNER_UNAVAILABLE)

    assert _points(reader, "omnigent.server.errors") == [
        ({"error.code": ErrorCode.RUNNER_UNAVAILABLE}, 1)
    ]


def test_declared_opentelemetry_floor_supports_bucket_advisory() -> None:
    # Server startup passes explicit_bucket_boundaries_advisory, added in 1.23.
    pyproject = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text("utf-8"))
    project = pyproject["project"]
    requirements = [
        Requirement(spec)
        for spec in [*project["dependencies"], *chain(*project["optional-dependencies"].values())]
    ]
    too_old = {
        str(req)
        for req in requirements
        if req.name in {"opentelemetry-api", "opentelemetry-sdk"}
        and req.specifier.contains("1.22.0")
    }
    assert too_old == set()
