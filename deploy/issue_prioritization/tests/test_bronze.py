from __future__ import annotations

from datetime import UTC, datetime

import pytest

from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.classification import Classification
from issue_prioritization.databricks_io import SparkIssueSource
from issue_prioritization.domain import IssueType, Priority, Severity


def test_bronze_adapter_accepts_github_structs_and_json() -> None:
    issue = BronzeIssue.from_mapping(
        {
            "issue_number": 42,
            "title": "Android login fails",
            "body": "OIDC redirect does not return",
            "html_url": "https://github.com/omnigent-ai/omnigent/issues/42",
            "user_login": "community",
            "labels": '[{"name":"Bug"},{"name":"P1-high"}]',
            "created_at": "2026-08-01T00:00:00Z",
            "reactions": {"total_count": 3},
        }
    )
    classification = Classification(
        issue_number=42,
        issue_type=IssueType.BUG,
        severity=Severity.S1,
        area_keys=("android",),
        component_labels=("comp:android",),
        reasoning="No login workaround",
        content_hash=issue.content().content_hash,
    )

    normalized = issue.to_issue(classification, datetime(2026, 8, 5, tzinfo=UTC))

    assert issue.labels == ("Bug", "P1-high")
    assert issue.reaction_count == 3
    assert normalized.current_priority == Priority.P1
    assert normalized.age_days == 4


def test_spark_source_rejects_unquoted_table_expressions() -> None:
    with pytest.raises(ValueError, match="catalog.schema.table"):
        SparkIssueSource(object(), "main.schema.issues WHERE true")
