from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from issue_prioritization.artifacts import RankedIssue, write_artifacts
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.classification import Classification
from issue_prioritization.comments import build_triage_comment
from issue_prioritization.config import ScoringConfig
from issue_prioritization.domain import (
    EvidenceKind,
    Impact,
    InformationStatus,
    IssueType,
    MissingInformation,
)
from issue_prioritization.mutations import BotState, MutationPlan
from issue_prioritization.pipeline import PipelineRun

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){2}$")
_CLASSIFICATION_SCHEMA = """issue_number BIGINT, issue_type STRING, impact STRING,
area_keys ARRAY<STRING>, component_labels ARRAY<STRING>, reasoning STRING,
content_hash STRING, reported_type STRING, evidence_kind STRING,
information_status STRING, missing_information ARRAY<STRING>"""
_SCORE_SCHEMA = """run_id STRING, mode STRING, regrade BOOLEAN,
adopt_legacy_bot_priorities BOOLEAN, legacy_priorities_adopted BIGINT,
scored_at TIMESTAMP, rank BIGINT, previous_rank BIGINT, rank_delta BIGINT,
issue_number BIGINT, title STRING, url STRING, issue_type STRING, impact STRING,
classification_reasoning STRING, score DOUBLE, upvote_count BIGINT, duplicate_count BIGINT,
current_priority STRING, proposed_priority STRING,
reported_type STRING, type_label_mismatch BOOLEAN, evidence_kind STRING,
information_status STRING, missing_information ARRAY<STRING>,
area_keys ARRAY<STRING>, component_labels ARRAY<STRING>, breakdown_json STRING,
labels_add ARRAY<STRING>, labels_remove ARRAY<STRING>, mutation_blocked ARRAY<STRING>"""
_BOT_STATE_SCHEMA = """issue_number BIGINT, priority STRING, components ARRAY<STRING>"""


class SparkIssueSource:
    def __init__(self, spark: object, table: str, repo: str) -> None:
        self.spark = spark
        self.table = _table(table)
        self.repo = repo

    def load_open_issues(self) -> list[BronzeIssue]:
        frame = self.spark.table(self.table)
        rows = frame.where("state = 'open'").drop("state").collect()
        issues = []
        for row in rows:
            value = {**row.asDict(recursive=True), "state": "open"}
            if value.get("repo") != self.repo:
                continue
            issue = BronzeIssue.from_mapping(value)
            if not issue.is_pull_request:
                issues.append(issue)
        return issues


class SparkClassificationRepository:
    def __init__(self, spark: object, table: str) -> None:
        self.spark = spark
        self.table = _table(table)

    def load(self) -> dict[int, Classification]:
        if not self.spark.catalog.tableExists(self.table):
            return {}
        rows = self.spark.table(self.table).collect()
        return {int(row.issue_number): _classification_from_row(row) for row in rows}

    def upsert(self, classifications: list[Classification]) -> None:
        rows = [
            {
                "issue_number": item.issue_number,
                "issue_type": item.issue_type.label,
                "impact": item.impact.value,
                "area_keys": list(item.area_keys),
                "component_labels": list(item.component_labels),
                "reasoning": item.reasoning,
                "content_hash": item.content_hash,
                "reported_type": item.reported_type.label if item.reported_type else None,
                "evidence_kind": item.evidence_kind.value,
                "information_status": item.information_status.value,
                "missing_information": [value.value for value in item.missing_information],
            }
            for item in classifications
        ]
        if not self.spark.catalog.tableExists(self.table):
            frame = self.spark.createDataFrame(rows, schema=_CLASSIFICATION_SCHEMA)
            frame.write.format("delta").mode("overwrite").saveAsTable(self.table)
            return
        schema = self.spark.table(self.table).schema
        definitions = {
            "reported_type": "STRING",
            "evidence_kind": "STRING",
            "information_status": "STRING",
            "missing_information": "ARRAY<STRING>",
        }
        missing_columns = [name for name in definitions if name not in _field_names(schema)]
        if missing_columns:
            columns = ", ".join(f"{name} {definitions[name]}" for name in missing_columns)
            self.spark.sql(f"ALTER TABLE {self.table} ADD COLUMNS ({columns})")
            schema = self.spark.table(self.table).schema
        if "impact" not in _field_names(schema) and "severity" in _field_names(schema):
            rows = [
                {
                    **{key: value for key, value in row.items() if key != "impact"},
                    "severity": Impact.parse(row["impact"]).legacy_code,
                }
                for row in rows
            ]
        frame = self.spark.createDataFrame(rows, schema=schema)
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
                    "issue_type": issue.issue_type.label,
                    "impact": issue.impact.value,
                    "classification_reasoning": issue.classification_reasoning,
                    "reported_type": issue.reported_type.label if issue.reported_type else None,
                    "type_label_mismatch": bool(
                        issue.reported_type and issue.reported_type != issue.issue_type
                    ),
                    "evidence_kind": issue.evidence_kind.value,
                    "information_status": issue.information_status.value,
                    "missing_information": [value.value for value in issue.missing_information],
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
        ranked = {item.issue.number: item for item in run.ranked}
        metadata = {
            "run_id": run.run_id,
            "mode": run.mode.value,
            "regrade": run.regrade,
            "adopt_legacy_bot_priorities": run.adopt_legacy_bot_priorities,
            "legacy_priorities_adopted": run.legacy_priorities_adopted,
            "scored_at": run.scored_at.isoformat(),
            "classifications_updated": run.classifications_updated,
            "classification_failures": [
                {"issue_number": failure.issue_number, "reason": failure.reason}
                for failure in run.classification_failures
            ],
        }
        mutations = [
            {
                "issue_number": plan.target.issue_number,
                "target": {
                    "priority": plan.target.priority,
                    "components": list(plan.target.components),
                    "issue_type": plan.target.issue_type,
                    "needs_info": plan.target.needs_info,
                },
                "labels_add": list(plan.labels_add),
                "labels_remove": list(plan.labels_remove),
                "blocked": list(plan.blocked),
                "next_bot_state": {
                    "priority": plan.next_state.priority,
                    "components": list(plan.next_state.components),
                },
                "comment": build_triage_comment(
                    ranked[plan.target.issue_number],
                    plan,
                    _planned_labels_after(ranked[plan.target.issue_number], plan),
                    run.scored_at,
                ),
            }
            for plan in run.mutations
        ]
        (destination / "mutations.json").write_text(json.dumps(mutations, indent=2) + "\n")
        pending_metadata = destination / ".run.json.tmp"
        pending_metadata.write_text(json.dumps(metadata, indent=2) + "\n")
        pending_metadata.replace(destination / "run.json")


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
                components=tuple(row.components or ()),
            )
            for row in self.spark.table(self.table).collect()
        }

    def upsert(self, states: list[BotState]) -> None:
        rows = [
            {
                "issue_number": state.issue_number,
                "priority": state.priority,
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


def _row_value(row: object, *names: str) -> object:
    for name in names:
        value = getattr(row, name, None)
        if value is not None:
            return value
    raise ValueError(f"row does not contain any of {names}")


def _classification_from_row(row: object) -> Classification:
    required_fields = ("evidence_kind", "information_status", "missing_information")
    needs_regrade = not hasattr(row, "reported_type") or any(
        getattr(row, name, None) is None for name in required_fields
    )
    reported_type = getattr(row, "reported_type", None)
    return Classification(
        issue_number=int(row.issue_number),
        issue_type=IssueType.parse(row.issue_type),
        impact=Impact.parse(_row_value(row, "impact", "severity")),
        area_keys=tuple(row.area_keys or ()),
        component_labels=tuple(row.component_labels or ()),
        reasoning=str(row.reasoning or ""),
        content_hash="" if needs_regrade else str(row.content_hash),
        reported_type=IssueType.parse(reported_type) if reported_type else None,
        evidence_kind=EvidenceKind.parse(getattr(row, "evidence_kind", None) or EvidenceKind.NONE),
        information_status=InformationStatus.parse(
            getattr(row, "information_status", None) or InformationStatus.NOT_APPLICABLE
        ),
        missing_information=tuple(
            MissingInformation.parse(item)
            for item in (getattr(row, "missing_information", None) or ())
        ),
    )


def _field_names(schema: object) -> set[str]:
    field_names = getattr(schema, "fieldNames", None)
    if callable(field_names):
        return set(field_names())
    return {str(field.name) for field in getattr(schema, "fields", ())}


def _planned_labels_after(item: RankedIssue, plan: MutationPlan) -> tuple[str, ...]:
    current_priority = item.issue.current_priority
    labels = {current_priority.value} if current_priority else set()
    labels = (labels - set(plan.labels_remove)) | set(plan.labels_add)
    return tuple(sorted(labels))
