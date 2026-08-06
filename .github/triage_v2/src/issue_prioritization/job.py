from __future__ import annotations

import argparse
from pathlib import Path

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.config import ScoringConfig
from issue_prioritization.databricks_io import (
    SparkBotStateRepository,
    SparkClassificationRepository,
    SparkIssueSource,
    SparkScoreSink,
    VolumeArtifactSink,
)
from issue_prioritization.github import (
    GitHubClient,
    GitHubLegacyPriorityOwnership,
    GitHubMutationSink,
)
from issue_prioritization.labels import LabelManifest
from issue_prioritization.model_serving import serving_endpoint_classifier
from issue_prioritization.mutations import MutationPlanner
from issue_prioritization.pipeline import IssuePrioritizationPipeline, PipelineMode
from issue_prioritization.scoring import ScoreEngine


def _enabled(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


def _print_classification_progress(completed: int, total: int) -> None:
    if completed == 0:
        print(f"Refreshing {total} issue classifications", flush=True)
    elif completed % 10 == 0 or completed == total:
        print(f"Classified {completed}/{total} issues", flush=True)


def validate_github_write_gate(
    mode: PipelineMode,
    allow_github_writes: str,
    github_secret_scope: str,
    adopt_legacy_bot_priorities: bool = False,
) -> None:
    if mode == PipelineMode.APPLY and not _enabled(allow_github_writes):
        raise RuntimeError("apply mode is disabled: allow_github_writes is false")
    if (mode == PipelineMode.APPLY or adopt_legacy_bot_priorities) and not github_secret_scope:
        raise RuntimeError("github_secret_scope is required for GitHub access")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=list(PipelineMode), default=PipelineMode.DRY_RUN)
    parser.add_argument("--regrade", default="false")
    parser.add_argument(
        "--adopt-legacy-bot-priorities",
        "--adopt_legacy_bot_priorities",
        default="false",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-table", required=True)
    parser.add_argument("--classifications-table", required=True)
    parser.add_argument("--scores-table", required=True)
    parser.add_argument("--latest-scores-view", required=True)
    parser.add_argument("--bot-state-table", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--model-endpoint", default="")
    parser.add_argument("--areas-path", required=True, type=Path)
    parser.add_argument("--maintainers-path", required=True, type=Path)
    parser.add_argument("--label-manifest-path", required=True, type=Path)
    parser.add_argument("--github-repo", required=True)
    parser.add_argument("--github-secret-scope", default="")
    parser.add_argument("--github-token-secret-key", default="github-token")
    parser.add_argument(
        "--legacy-priority-bot-logins",
        default="github-actions[bot],omnigent-ci[bot]",
    )
    parser.add_argument("--allow-github-writes", default="false")
    args = parser.parse_args()

    from pyspark.sql import SparkSession

    spark = SparkSession.getActiveSession()
    if spark is None:
        raise RuntimeError("issue-priority-job requires an active Spark session")

    config = ScoringConfig.default()
    areas = AreaCatalog.from_json(args.areas_path)
    manifest = LabelManifest.from_json(args.label_manifest_path)
    states = SparkBotStateRepository(spark, args.bot_state_table)
    mode = PipelineMode(args.mode)
    adopt_legacy = _enabled(args.adopt_legacy_bot_priorities)
    validate_github_write_gate(
        mode,
        args.allow_github_writes,
        args.github_secret_scope,
        adopt_legacy,
    )
    github_client = None
    if mode == PipelineMode.APPLY or adopt_legacy:
        from pyspark.dbutils import DBUtils

        token = DBUtils(spark).secrets.get(
            scope=args.github_secret_scope,
            key=args.github_token_secret_key,
        )
        github_client = GitHubClient(token, args.github_repo)
    legacy_priorities = None
    if adopt_legacy:
        if github_client is None:
            raise RuntimeError("legacy priority adoption requires a GitHub client")
        legacy_priorities = GitHubLegacyPriorityOwnership(
            github_client,
            {
                login.strip()
                for login in args.legacy_priority_bot_logins.split(",")
                if login.strip()
            },
        )
    planner = MutationPlanner(manifest, states, legacy_priorities)
    mutation_sink = None
    if mode == PipelineMode.APPLY:
        if github_client is None:
            raise RuntimeError("apply mode requires a GitHub client")
        mutation_sink = GitHubMutationSink(
            github_client,
            manifest,
            planner,
            states,
        )
    maintainers = {
        line.split("#", 1)[0].strip().lower()
        for line in args.maintainers_path.read_text().splitlines()
        if line.split("#", 1)[0].strip()
    }
    pipeline = IssuePrioritizationPipeline(
        source=SparkIssueSource(spark, args.source_table, args.github_repo),
        classifier=serving_endpoint_classifier(args.model_endpoint, areas),
        classifications=SparkClassificationRepository(spark, args.classifications_table),
        scores=SparkScoreSink(spark, args.scores_table, args.latest_scores_view),
        artifacts=VolumeArtifactSink(args.artifact_dir, config),
        engine=ScoreEngine(config, areas),
        maintainers=maintainers,
        mutation_planner=planner,
        mutation_sink=mutation_sink,
        classification_progress=_print_classification_progress,
    )
    run = pipeline.run(
        args.run_id,
        mode,
        regrade=_enabled(args.regrade),
        adopt_legacy_bot_priorities=adopt_legacy,
    )
    print(
        f"Scored {len(run.ranked)} issues; "
        f"refreshed {run.classifications_updated} classifications; "
        f"artifacts: {args.artifact_dir}/{run.run_id}"
    )


if __name__ == "__main__":
    main()
