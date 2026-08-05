from __future__ import annotations

import argparse
from pathlib import Path

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.config import ScoringConfig
from issue_prioritization.databricks_io import (
    SparkClassificationRepository,
    SparkIssueSource,
    SparkScoreSink,
    VolumeArtifactSink,
    ai_query_classifier,
)
from issue_prioritization.pipeline import IssuePrioritizationPipeline, PipelineMode
from issue_prioritization.scoring import ScoreEngine


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=[PipelineMode.DRY_RUN], default=PipelineMode.DRY_RUN)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-table", required=True)
    parser.add_argument("--classifications-table", required=True)
    parser.add_argument("--scores-table", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--model-endpoint", default="")
    parser.add_argument("--areas-path", required=True, type=Path)
    parser.add_argument("--maintainers-path", required=True, type=Path)
    args = parser.parse_args()

    from pyspark.sql import SparkSession

    spark = SparkSession.getActiveSession()
    if spark is None:
        raise RuntimeError("issue-priority-job requires an active Spark session")

    config = ScoringConfig.default()
    areas = AreaCatalog.from_json(args.areas_path)
    maintainers = {
        line.split("#", 1)[0].strip().lower()
        for line in args.maintainers_path.read_text().splitlines()
        if line.split("#", 1)[0].strip()
    }
    pipeline = IssuePrioritizationPipeline(
        source=SparkIssueSource(spark, args.source_table),
        classifier=ai_query_classifier(spark, args.model_endpoint, areas),
        classifications=SparkClassificationRepository(spark, args.classifications_table),
        scores=SparkScoreSink(spark, args.scores_table),
        artifacts=VolumeArtifactSink(args.artifact_dir, config),
        engine=ScoreEngine(config, areas),
        maintainers=maintainers,
    )
    run = pipeline.run(args.run_id, PipelineMode(args.mode))
    print(
        f"Scored {len(run.ranked)} issues; "
        f"refreshed {run.classifications_updated} classifications; "
        f"artifacts: {args.artifact_dir}/{run.run_id}"
    )


if __name__ == "__main__":
    main()
