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
from issue_prioritization.pipeline import PipelineRun

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){2}$")
_CLASSIFICATION_SCHEMA = """issue_number BIGINT, issue_type STRING, severity STRING,
area_keys ARRAY<STRING>, component_labels ARRAY<STRING>, reasoning STRING,
content_hash STRING"""
_SCORE_SCHEMA = """run_id STRING, mode STRING, scored_at TIMESTAMP, rank BIGINT,
previous_rank BIGINT, rank_delta BIGINT, issue_number BIGINT, title STRING, url STRING,
issue_type STRING, severity STRING, score DOUBLE, current_priority STRING,
proposed_priority STRING, area_keys ARRAY<STRING>, component_labels ARRAY<STRING>,
breakdown_json STRING"""


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
        frame = self.spark.createDataFrame(rows, schema=_CLASSIFICATION_SCHEMA)
        if not self.spark.catalog.tableExists(self.table):
            frame.write.format("delta").mode("overwrite").saveAsTable(self.table)
            return
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
    def __init__(self, spark: object, table: str) -> None:
        self.spark = spark
        self.table = _table(table)

    def write(self, run: PipelineRun) -> None:
        rows = []
        for item in run.ranked:
            issue = item.issue
            result = item.result
            rows.append(
                {
                    "run_id": run.run_id,
                    "mode": run.mode.value,
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
                    "current_priority": issue.current_priority.value
                    if issue.current_priority
                    else None,
                    "proposed_priority": result.priority.value,
                    "area_keys": list(issue.area_keys),
                    "component_labels": list(issue.component_labels),
                    "breakdown_json": json.dumps(
                        [asdict(step) for step in result.steps], default=str
                    ),
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
            "scored_at": run.scored_at.isoformat(),
            "classifications_updated": run.classifications_updated,
        }
        (destination / "run.json").write_text(json.dumps(metadata, indent=2) + "\n")


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
