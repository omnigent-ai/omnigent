"""Tests for Inbox item chart validation."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnigent.server.inbox_chart import InboxChartV1

_CHART: dict[str, Any] = {
    "version": 1,
    "type": "line",
    "title": "p90 launch time",
    "unit": "s",
    "x": ["11-01", "11-02", "11-03"],
    "series": [{"name": "p90", "values": [12.1, None, 8.3], "emphasis": True}],
    "highlight": {"from": "11-02", "to": "11-03"},
    "markers": [{"x": "11-02", "label": "PR merged"}],
    "source": {"label": "launch dashboard", "href": "https://example.test/d"},
}


def test_a_valid_chart_round_trips_with_wire_names() -> None:
    assert InboxChartV1.model_validate(_CHART).stored() == _CHART


def _with(path: str, value: Any) -> dict[str, Any]:
    chart = copy.deepcopy(_CHART)
    *parents, leaf = path.split(".")
    target: Any = chart
    for part in parents:
        target = target[int(part)] if part.isdigit() else target[part]
    if isinstance(target, list):
        target[int(leaf)] = value
    else:
        target[leaf] = value
    return chart


@pytest.mark.parametrize(
    "chart",
    [
        _with("version", 2),
        _with("type", "pie"),
        _with("x", [f"d{n}" for n in range(61)]),
        _with("series", [{"name": f"s{n}", "values": [1, 2, 3]} for n in range(5)]),
        _with("series.0.values", [1.0, 2.0]),
        _with("series.0.values", [1.0, float("nan"), 2.0]),
        _with("markers", [{"x": "11-01", "label": "m"}] * 4),
        _with("highlight", {"from": "10-31", "to": "11-03"}),
        _with("markers", [{"x": "12-25", "label": "later"}]),
        _with("source", {"label": "s", "href": "javascript:alert(1)"}),
        _with("title", "t" * 121),
        _with("x", ["x" * 10_000, "b", "c"]),
    ],
    ids=[
        "unknown-version",
        "unknown-type",
        "too-many-x-labels",
        "too-many-series",
        "series-length-mismatch",
        "non-finite-value",
        "too-many-markers",
        "highlight-outside-x",
        "marker-outside-x",
        "non-web-source",
        "long-title",
        "long-label",
    ],
)
def test_invalid_charts_are_rejected(chart: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        InboxChartV1.model_validate(chart)


def test_charts_over_the_size_limit_are_rejected() -> None:
    # Field caps keep ASCII charts small; escaped non-ASCII labels can still exceed the limit.
    x = [f"{chr(0x4E00 + n) * 38}{n:02d}" for n in range(60)]
    series = [{"name": "n" * 60, "values": [-1.2345678901234567e300] * 60} for _ in range(4)]
    chart = {**_CHART, "x": x, "series": series, "markers": []}
    del chart["highlight"]

    with pytest.raises(ValidationError, match="bytes of JSON"):
        InboxChartV1.model_validate(chart)
