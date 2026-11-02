"""Dimension counts follow headline policy without rewriting the dashboard layout."""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from dev.reliability_dashboard import reconcile_dimensions

_HEADLINE = """WITH
selected_facts AS (
SELECT * FROM facts
),
window_included AS (
SELECT session_id, 'last_7d' AS window_name, NULL AS first_included_error_time
FROM selected_facts WHERE eligible = 1
),
window_rates AS (
SELECT count(*) FROM window_included
)
SELECT * FROM window_rates
"""
_AGGREGATION = """kpi_dimension_rates AS (
SELECT harness AS dimension, count(*) AS active_sessions, sum(is_error) AS errors
FROM kpi_session_versions GROUP BY harness
UNION ALL
SELECT runner_version, count(*), sum(is_error)
FROM kpi_session_versions GROUP BY runner_version
)
SELECT * FROM kpi_dimension_rates ORDER BY dimension
"""
_DIMENSIONS = (
    """WITH
selected_facts AS (
SELECT * FROM facts
),
kpi_session_versions AS (
SELECT session_id, harness, is_kpi_error AS is_error, runner_version
FROM selected_facts WHERE record_type='version_session' AND report_days=7
),
"""
    + _AGGREGATION
)


def _dashboard() -> dict:
    return {
        "datasets": [
            {"name": "mv_compare_kpi3_errors", "queryLines": [_HEADLINE]},
            {
                "name": "mv_compare_kpi3_dimension_rates",
                "queryLines": [_DIMENSIONS],
                "columns": ["dimension", "active_sessions", "errors"],
                "parameters": ["selected_harnesses", "lookback_days"],
            },
            {"name": "unrelated", "queryLines": ["SELECT 42"]},
        ],
        "pages": [{"name": "reliability", "layout": [{"widget": "harness-rate"}]}],
    }


@pytest.mark.parametrize("wrapped", [False, True])
def test_reconciliation_preserves_export_and_uses_current_policy(wrapped: bool) -> None:
    dashboard = _dashboard()
    exported = (
        {"serialized_dashboard": json.dumps(dashboard), "etag": "unchanged"}
        if wrapped
        else dashboard
    )
    original = copy.deepcopy(exported)
    result = reconcile_dimensions(exported)
    assert exported == original
    document = json.loads(result["serialized_dashboard"]) if wrapped else result
    assert document["pages"] == dashboard["pages"]
    assert document["datasets"][0] == dashboard["datasets"][0]
    assert document["datasets"][2] == dashboard["datasets"][2]
    target = document["datasets"][1]
    assert target["columns"] == dashboard["datasets"][1]["columns"]
    assert target["parameters"] == dashboard["datasets"][1]["parameters"]
    if wrapped:
        assert result["etag"] == "unchanged"
    query = "".join(target["queryLines"])
    assert query.endswith(_AGGREGATION)
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE facts(session_id, harness, runner_version, server_version, "
            "record_type, report_days, is_kpi_error, eligible)"
        )
        # Resumed/excluded failures, a genuine failure, and a newly eligible cause.
        connection.executemany(
            "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("resumed", "claude", "new", "server", "version_session", 7, 1, 0),
                ("excluded", "codex", "old", "server", "version_session", 7, 1, 0),
                ("failed", "claude", "new", "server", "version_session", 7, 1, 1),
                ("new-policy", "codex", "old", "server", "version_session", 7, 0, 1),
            ],
        )
        assert connection.execute(_DIMENSIONS).fetchall() == [
            ("claude", 2, 2),
            ("codex", 2, 1),
            ("new", 2, 2),
            ("old", 2, 1),
        ]
        assert connection.execute(query).fetchall() == [
            ("claude", 2, 1),
            ("codex", 2, 1),
            ("new", 2, 1),
            ("old", 2, 1),
        ]
    assert reconcile_dimensions(result) == result


@pytest.mark.parametrize(
    "drift", ["missing-policy", "duplicate-boundary", "duplicate-dataset", "missing-lines"]
)
def test_refuses_unrecognized_or_ambiguous_export(drift: str) -> None:
    exported = _dashboard()
    if drift == "missing-policy":
        exported["datasets"][0]["queryLines"] = [
            _HEADLINE.replace("window_included AS", "other AS")
        ]
    elif drift == "duplicate-boundary":
        exported["datasets"][0]["queryLines"] = [_HEADLINE + "\nwindow_rates AS (SELECT 1)"]
    elif drift == "duplicate-dataset":
        exported["datasets"].append(copy.deepcopy(exported["datasets"][1]))
    else:
        exported["datasets"][1].pop("queryLines")
    with pytest.raises(ValueError):
        reconcile_dimensions(exported)
