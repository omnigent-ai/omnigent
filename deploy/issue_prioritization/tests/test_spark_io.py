from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from issue_prioritization.artifacts import RankedIssue
from issue_prioritization.classification import Classification
from issue_prioritization.databricks_io import (
    SparkBotStateRepository,
    SparkClassificationRepository,
    SparkScoreSink,
)
from issue_prioritization.domain import (
    Issue,
    IssueType,
    Priority,
    ScoreResult,
    Severity,
)
from issue_prioritization.mutations import BotState
from issue_prioritization.pipeline import PipelineMode, PipelineRun


class FakeCatalog:
    def tableExists(self, table):
        return False


class FakeWriter:
    def __init__(self):
        self.options = {}
        self.table = None

    def format(self, value):
        return self

    def option(self, name, value):
        self.options[name] = value
        return self

    def mode(self, value):
        return self

    def saveAsTable(self, table):
        self.table = table


class FakeFrame:
    def __init__(self):
        self.write = FakeWriter()


class FakeSpark:
    def __init__(self):
        self.catalog = FakeCatalog()
        self.schemas = []
        self.frames = []

    def createDataFrame(self, rows, schema):
        self.schemas.append(schema)
        frame = FakeFrame()
        self.frames.append(frame)
        return frame


def test_classification_schema_handles_empty_arrays() -> None:
    spark = FakeSpark()
    repository = SparkClassificationRepository(spark, "main.team.classifications")
    classification = Classification(
        issue_number=1,
        issue_type=IssueType.BUG,
        severity=Severity.S3,
        area_keys=(),
        component_labels=(),
        reasoning="Unknown",
        content_hash="hash",
    )

    repository.upsert([classification])

    assert spark.schemas[0].count("ARRAY<STRING>") == 2


def test_score_sink_uses_schema_evolution() -> None:
    spark = FakeSpark()
    sink = SparkScoreSink(spark, "main.team.scores")
    issue = Issue(1, "Title", "url", IssueType.BUG, Severity.S3)
    ranked = RankedIssue(
        rank=1,
        previous_rank=1,
        issue=issue,
        result=ScoreResult(Decimal("10"), Priority.P3, ()),
    )
    run = PipelineRun(
        "run",
        PipelineMode.DRY_RUN,
        datetime.now(UTC),
        (ranked,),
        0,
        (),
    )

    sink.write(run)

    assert spark.schemas[0].count("ARRAY<STRING>") == 5
    assert spark.frames[0].write.options == {"mergeSchema": "true"}


def test_bot_state_schema_handles_empty_ownership() -> None:
    spark = FakeSpark()
    repository = SparkBotStateRepository(spark, "main.team.bot_state")

    repository.upsert([BotState(1, None, None, ())])

    assert "components ARRAY<STRING>" in spark.schemas[0]
