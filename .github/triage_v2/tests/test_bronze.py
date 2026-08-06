from __future__ import annotations

import json
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
            "user_login": "community",
            "labels": '[{"name":"Bug"},{"name":"P1-high"}]',
            "created_at": "2026-08-01T00:00:00Z",
            "raw_json": json.dumps(
                {
                    "html_url": "https://github.com/omnigent-ai/omnigent/issues/42",
                    "reactions": {"total_count": 5, "+1": 3, "-1": 2},
                }
            ),
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
    assert issue.url == "https://github.com/omnigent-ai/omnigent/issues/42"
    assert issue.upvote_count == 3
    assert normalized.current_priority == Priority.P1
    assert normalized.age_days == 4


def test_bronze_adapter_does_not_count_non_upvote_reactions() -> None:
    issue = BronzeIssue.from_mapping(
        {
            "number": 42,
            "title": "Android login fails",
            "created_at": "2026-08-01T00:00:00Z",
            "reactions": {"total_count": 4, "-1": 2, "confused": 2},
        }
    )

    assert issue.upvote_count == 0


def test_spark_source_rejects_unquoted_table_expressions() -> None:
    with pytest.raises(ValueError, match="catalog.schema.table"):
        SparkIssueSource(object(), "main.schema.issues WHERE true", "org/repo")


def test_spark_source_filters_repository_and_pull_requests() -> None:
    base = {
        "issue_number": 42,
        "title": "Android login fails",
        "created_at": "2026-08-01T00:00:00Z",
        "state": "open",
        "repo": "omnigent-ai/omnigent",
        "raw_json": json.dumps({"html_url": "https://github.com/issues/42"}),
    }

    class Row:
        def __init__(self, value):
            self.value = value

        def asDict(self, recursive=True):
            return self.value

    class Frame:
        def where(self, expression):
            assert expression == "state = 'open'"
            return self

        def collect(self):
            return [
                Row(base),
                Row({**base, "issue_number": 43, "repo": "other/repo"}),
                Row(
                    {
                        **base,
                        "issue_number": 44,
                        "raw_json": json.dumps(
                            {
                                "html_url": "https://github.com/pull/44",
                                "pull_request": {"url": "https://api.github.com/pulls/44"},
                            }
                        ),
                    }
                ),
            ]

    class Spark:
        def table(self, table):
            assert table == "main.team.issues"
            return Frame()

    source = SparkIssueSource(Spark(), "main.team.issues", "omnigent-ai/omnigent")
    issues = source.load_open_issues()

    assert [issue.number for issue in issues] == [42]
