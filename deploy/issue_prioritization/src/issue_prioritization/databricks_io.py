from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from issue_prioritization.artifacts import write_artifacts
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.classification import Classification, PromptClassifier
from issue_prioritization.config import ScoringConfig
from issue_prioritization.domain import IssueType, Severity
from issue_prioritization.mutations import BotState
from issue_prioritization.pipeline import PipelineRun

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){2}$")
_CLASSIFICATION_SCHEMA = """issue_number BIGINT, issue_type STRING, severity STRING,
area_keys ARRAY<STRING>, component_labels ARRAY<STRING>, reasoning STRING,
content_hash STRING"""
_SCORE_SCHEMA = """run_id STRING, mode STRING, regrade BOOLEAN,
adopt_legacy_bot_priorities BOOLEAN, legacy_priorities_adopted BIGINT,
scored_at TIMESTAMP, rank BIGINT, previous_rank BIGINT, rank_delta BIGINT,
issue_number BIGINT, title STRING, url STRING, issue_type STRING, severity STRING,
score DOUBLE, upvote_count BIGINT, duplicate_count BIGINT,
current_priority STRING, proposed_priority STRING,
area_keys ARRAY<STRING>, component_labels ARRAY<STRING>, breakdown_json STRING,
labels_add ARRAY<STRING>, labels_remove ARRAY<STRING>, mutation_blocked ARRAY<STRING>"""
_BOT_STATE_SCHEMA = """issue_number BIGINT, priority STRING, severity STRING,
components ARRAY<STRING>"""


class SparkIssueSource:
    def __init__(self, spark: object, table: str) -> None:
        self.spark = spark
        self.table = _table(table)

    def load_open_issues(self) -> list[BronzeIssue]:
        frame = self.spark.table(self.table)
        rows = frame.where("state = 'open'").collect()
        issues = []
        for row in rows:
            value = row.asDict(recursive=True)
            if value.get("pull_request"):
                continue
            issues.append(BronzeIssue.from_mapping(value))
        return issues


class SparkClassificationRepository:
    def __init__(self, spark: object, table: str) -> None:
        self.spark = spark
        self.table = _table(table)

    def load(self) -> dict[int, Classification]:
        if not self.spark.catalog.tableExists(self.table):
            return {}
        rows = self.spark.table(self.table).collect()
        return {
            int(row.issue_number): Classification(
                issue_number=int(row.issue_number),
                issue_type=IssueType(str(row.issue_type)),
                severity=Severity(str(row.severity)),
                area_keys=tuple(row.area_keys or ()),
                component_labels=tuple(row.component_labels or ()),
                reasoning=str(row.reasoning or ""),
                content_hash=str(row.content_hash),
            )
            for row in rows
        }

    def upsert(self, classifications: list[Classification]) -> None:
        rows = [
            {
                "issue_number": item.issue_number,
                "issue_type": item.issue_type.value,
                "severity": item.severity.value,
                "area_keys": list(item.area_keys),
                "component_labels": list(item.component_labels),
                "reasoning": item.reasoning,
                "content_hash": item.content_hash,
            }
            for item in classifications
        ]
        if not self.spark.catalog.tableExists(self.table):
            frame = self.spark.createDataFrame(rows, schema=_CLASSIFICATION_SCHEMA)
            frame.write.format("delta").mode("overwrite").saveAsTable(self.table)
            return
        frame = self.spark.createDataFrame(rows, schema=self.spark.table(self.table).schema)
        view = "issue_priority_classification_updates"
        frame.createOrReplaceTempView(view)
        self.spark.sql(
            f"""MERGE INTO {self.table} target
            USING {view} source
            ON target.issue_number = source.issue_number
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *"""
        )


class SparkScoreSink:
    def __init__(self, spark: object, table: str, latest_view: str) -> None:
        self.spark = spark
        self.table = _table(table)
        self.latest_view = _table(latest_view)

    def write(self, run: PipelineRun) -> None:
        mutations = {plan.target.issue_number: plan for plan in run.mutations}
        rows = []
        for item in run.ranked:
            issue = item.issue
            result = item.result
            mutation = mutations.get(issue.number)
            rows.append(
                {
                    "run_id": run.run_id,
                    "mode": run.mode.value,
                    "regrade": run.regrade,
                    "adopt_legacy_bot_priorities": run.adopt_legacy_bot_priorities,
                    "legacy_priorities_adopted": run.legacy_priorities_adopted,
                    "scored_at": run.scored_at,
                    "rank": item.rank,
                    "previous_rank": item.previous_rank,
                    "rank_delta": item.rank_delta,
                    "issue_number": issue.number,
                    "title": issue.title,
                    "url": issue.url,
                    "issue_type": issue.issue_type.value,
                    "severity": issue.severity.value,
                    "score": float(result.score),
                    "upvote_count": issue.upvote_count,
                    "duplicate_count": issue.duplicate_count,
                    "current_priority": issue.current_priority.value
                    if issue.current_priority
                    else None,
                    "proposed_priority": result.priority.value,
                    "area_keys": list(issue.area_keys),
                    "component_labels": list(issue.component_labels),
                    "breakdown_json": json.dumps(
                        [asdict(step) for step in result.steps], default=str
                    ),
                    "labels_add": list(mutation.labels_add) if mutation else [],
                    "labels_remove": list(mutation.labels_remove) if mutation else [],
                    "mutation_blocked": list(mutation.blocked) if mutation else [],
                }
            )
        if rows:
            (
                self.spark.createDataFrame(rows, schema=_SCORE_SCHEMA)
                .write.format("delta")
                .option("mergeSchema", "true")
                .mode("append")
                .saveAsTable(self.table)
            )
            self.spark.sql(latest_scores_view_sql(self.table, self.latest_view))


class VolumeArtifactSink:
    def __init__(self, root: str, config: ScoringConfig) -> None:
        self.root = Path(root)
        self.config = config

    def write(self, run: PipelineRun) -> None:
        destination = self.root / run.run_id
        write_artifacts(destination, list(run.ranked), self.config)
        metadata = {
            "run_id": run.run_id,
            "mode": run.mode.value,
            "regrade": run.regrade,
            "adopt_legacy_bot_priorities": run.adopt_legacy_bot_priorities,
            "legacy_priorities_adopted": run.legacy_priorities_adopted,
            "scored_at": run.scored_at.isoformat(),
            "classifications_updated": run.classifications_updated,
        }
        (destination / "run.json").write_text(json.dumps(metadata, indent=2) + "\n")
        mutations = [
            {
                "issue_number": plan.target.issue_number,
                "target": {
                    "priority": plan.target.priority,
                    "severity": plan.target.severity,
                    "components": list(plan.target.components),
                },
                "labels_add": list(plan.labels_add),
                "labels_remove": list(plan.labels_remove),
                "blocked": list(plan.blocked),
                "next_bot_state": {
                    "priority": plan.next_state.priority,
                    "severity": plan.next_state.severity,
                    "components": list(plan.next_state.components),
                },
            }
            for plan in run.mutations
        ]
        (destination / "mutations.json").write_text(json.dumps(mutations, indent=2) + "\n")


class SparkBotStateRepository:
    def __init__(self, spark: object, table: str) -> None:
        self.spark = spark
        self.table = _table(table)

    def load(self) -> dict[int, BotState]:
        if not self.spark.catalog.tableExists(self.table):
            return {}
        return {
            int(row.issue_number): BotState(
                issue_number=int(row.issue_number),
                priority=str(row.priority) if row.priority else None,
                severity=str(row.severity) if row.severity else None,
                components=tuple(row.components or ()),
            )
            for row in self.spark.table(self.table).collect()
        }

    def upsert(self, states: list[BotState]) -> None:
        rows = [
            {
                "issue_number": state.issue_number,
                "priority": state.priority,
                "severity": state.severity,
                "components": list(state.components),
            }
            for state in states
        ]
        if not rows:
            return
        if not self.spark.catalog.tableExists(self.table):
            frame = self.spark.createDataFrame(rows, schema=_BOT_STATE_SCHEMA)
            frame.write.format("delta").mode("overwrite").saveAsTable(self.table)
            return
        frame = self.spark.createDataFrame(rows, schema=self.spark.table(self.table).schema)
        view = "issue_priority_bot_state_updates"
        frame.createOrReplaceTempView(view)
        self.spark.sql(
            f"""MERGE INTO {self.table} target
            USING {view} source
            ON target.issue_number = source.issue_number
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *"""
        )


def ai_query_classifier(spark: object, endpoint: str, areas: object) -> PromptClassifier:
    if not endpoint:
        raise ValueError("model_endpoint is required when issue classifications are missing")

    def query(prompt: str) -> str:
        row = spark.sql(
            "SELECT ai_query(:endpoint, :prompt) AS response",
            args={"endpoint": endpoint, "prompt": prompt},
        ).first()
        return str(row.response)

    return PromptClassifier(query, areas)


def _table(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"expected catalog.schema.table, got {value!r}")
    return value


def latest_scores_view_sql(scores_table: str, latest_view: str) -> str:
    scores_table = _table(scores_table)
    latest_view = _table(latest_view)
    return f"""CREATE OR REPLACE VIEW {latest_view} AS
    SELECT *
    FROM {scores_table}
    WHERE run_id = (SELECT max_by(run_id, scored_at) FROM {scores_table})"""
