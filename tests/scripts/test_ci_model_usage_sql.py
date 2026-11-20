"""Opt-in warehouse regression for the reference dataset, using session-only fixtures."""

import datetime
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    not os.environ.get("CI_MODEL_USAGE_SQL_PROFILE"),
    reason="Set CI_MODEL_USAGE_SQL_PROFILE and CI_MODEL_USAGE_SQL_WAREHOUSE to run",
)
def test_daily_cost_allocation_is_complete_and_timezone_independent():
    from databricks import sql
    from databricks.sdk import WorkspaceClient

    workspace = WorkspaceClient(profile=os.environ["CI_MODEL_USAGE_SQL_PROFILE"])
    query = (ROOT / "docs/ci-model-usage.sql").read_text()
    for source, fixture in {
        "system.ai_gateway.usage": "fixture_gateway",
        "system.serving.served_entities": "fixture_entities",
        "system.serving.endpoint_usage": "fixture_legacy",
        "system.billing.usage": "fixture_billing",
        "system.billing.list_prices": "fixture_prices",
    }.items():
        query = query.replace(source, fixture)
    query = query.replace("current_timestamp()", "TIMESTAMP '2026-10-30T03:00:00Z'")
    fixtures = (Path(__file__).parent / "fixtures/ci_model_usage.sql").read_text()
    results = []
    with (
        sql.connect(
            server_hostname=urlsplit(workspace.config.host).hostname,
            http_path="/sql/1.0/warehouses/" + os.environ["CI_MODEL_USAGE_SQL_WAREHOUSE"],
            credentials_provider=lambda: workspace.config.authenticate,
        ) as connection,
        connection.cursor() as cursor,
    ):
        for timezone in ("UTC", "America/Los_Angeles", "Asia/Kathmandu"):
            cursor.execute(f"SET TIME ZONE '{timezone}'")
            cursor.execute("SELECT current_timezone()")
            assert cursor.fetchone()[0] == timezone
            for statement in fixtures.split(";"):
                if statement.strip():
                    cursor.execute(statement)
            cursor.execute(query, parameters={"workspace_id": "fixture"})
            results.append(
                sorted(
                    [row.asDict() for row in cursor.fetchall()],
                    key=lambda row: (
                        row["usage_date"],
                        row["service_principal"] or "",
                        row["pipeline"],
                        row["endpoint_name"] or "",
                        row["route"],
                    ),
                )
            )

    assert results[0] == results[1] == results[2]
    rows = results[0]
    assert {row["usage_date"] for row in rows} == {
        datetime.date(2026, 8, 31),
        datetime.date(2026, 10, 29),
    }
    indexed = {
        (row["endpoint_name"], row["pipeline"], row["route"], row["service_principal"]): row
        for row in rows
    }
    polly = indexed["priced", "polly", "AI Gateway", "sp"]
    docs = indexed["priced", "docs", "AI Gateway", "sp"]
    assert (
        polly["requests"],
        polly["known_list_usd"],
        polly["estimated_list_usd"],
        polly["requests_without_allocated_cost"],
    ) == (2, 2, None, 1)
    assert (docs["requests"], docs["estimated_list_usd"]) == (1, 6)
    partial = indexed["partial-price", "polly", "AI Gateway", "sp"]
    assert (
        partial["known_list_usd"],
        partial["estimated_list_usd"],
        partial["incomplete_prices"],
    ) == (4, None, True)
    assert indexed["correction", "polly", "AI Gateway", "sp"]["estimated_list_usd"] == -2
    for route, pipeline in (("AI Gateway", "polly"), ("Legacy serving", "Untagged")):
        mixed = indexed["mixed", pipeline, route, "sp"]
        assert mixed["mixed_routes"] is True
        assert mixed["estimated_list_usd"] is None
    assert indexed["mixed", "Unallocated billing", "Billing only", "sp"]["estimated_list_usd"] == 5
    assert indexed["mixed", "Unallocated billing", "Billing only", None]["estimated_list_usd"] == 3
    assert indexed["legacy", "Untagged", "Legacy serving", "sp"]["estimated_list_usd"] == 4
    assert indexed["zero", "polly", "AI Gateway", "sp"]["estimated_list_usd"] is None
    assert indexed["zero", "Unallocated billing", "Billing only", "sp"]["estimated_list_usd"] == 7
    assert (
        indexed["billing-only", "Unallocated billing", "Billing only", "sp"]["estimated_list_usd"]
        == 6
    )
    unpriced = indexed["unpriced-only", "Unallocated billing", "Billing only", "sp"]
    assert unpriced["estimated_list_usd"] is None and unpriced["incomplete_prices"] is True
    assert sum(row["known_list_usd"] or 0 for row in rows) == 40
    assert sum(row["requests"] for row in rows) == 11
