from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.artifacts import RankedIssue, rank_issues
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.classification import Classification, Classifier
from issue_prioritization.comments import build_triage_comment
from issue_prioritization.config import ScoringConfig
from issue_prioritization.github import GitHubClient, GitHubMutationSink
from issue_prioritization.labels import LabelManifest
from issue_prioritization.model_serving import serving_endpoint_classifier
from issue_prioritization.mutations import (
    BotState,
    MutationPlan,
    MutationPlanner,
    MutationTarget,
    target_from_ranked,
)
from issue_prioritization.pipeline import PipelineMode, PipelineRun
from issue_prioritization.scoring import ScoreEngine


class MemoryBotStateRepository:
    def __init__(self) -> None:
        self.values: dict[int, BotState] = {}

    def load(self) -> dict[int, BotState]:
        return dict(self.values)

    def upsert(self, states: list[BotState]) -> None:
        self.values.update((state.issue_number, state) for state in states)


def prioritize_issue(
    issue: BronzeIssue,
    classifier: Classifier,
    config: ScoringConfig,
    areas: AreaCatalog,
    manifest: LabelManifest,
    run_id: str,
    mode: PipelineMode,
) -> tuple[PipelineRun, Classification, MutationPlanner, MemoryBotStateRepository]:
    scored_at = datetime.now(UTC)
    classification = classifier.classify(issue.content())
    states = MemoryBotStateRepository()
    planner = MutationPlanner(manifest, states)
    ranked = (
        _rank_issue(
            issue,
            classification,
            scored_at,
            issue.labels,
            ScoreEngine(config, areas),
        ),
    )
    plan = planner.plan_one(target_from_ranked(ranked[0]), issue.labels, None)
    return (
        PipelineRun(
            run_id=run_id,
            mode=mode,
            scored_at=scored_at,
            ranked=ranked,
            classifications_updated=1,
            mutations=(plan,),
        ),
        classification,
        planner,
        states,
    )


def _rank_issue(
    issue: BronzeIssue,
    classification: Classification,
    scored_at: datetime,
    labels: tuple[str, ...],
    engine: ScoreEngine,
) -> RankedIssue:
    live_issue = replace(issue, labels=labels)
    return rank_issues([live_issue.to_issue(classification, scored_at)], engine)[0]


def target_for_labels(
    issue: BronzeIssue,
    classification: Classification,
    scored_at: datetime,
    labels: tuple[str, ...],
    engine: ScoreEngine,
) -> MutationTarget:
    return target_from_ranked(_rank_issue(issue, classification, scored_at, labels, engine))


def write_event_artifacts(
    output_dir: Path,
    run: PipelineRun,
    classification: Classification,
    config: ScoringConfig,
    model_endpoint: str,
    source_revision: str,
    labels_before: tuple[str, ...],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "config.json").write_text(json.dumps(config.as_dict(), indent=2) + "\n")
    write_event_status(
        output_dir,
        run,
        classification,
        model_endpoint,
        source_revision,
        labels_before,
        status="planned",
    )


def write_event_status(
    output_dir: Path,
    run: PipelineRun,
    classification: Classification,
    model_endpoint: str,
    source_revision: str,
    labels_before: tuple[str, ...],
    *,
    status: str,
    labels_after: tuple[str, ...] | None = None,
    plan: MutationPlan | None = None,
    decision: RankedIssue | None = None,
    applied_bot_state: BotState | None = None,
) -> None:
    plan = plan or run.mutations[0]
    decision = decision or run.ranked[0]
    payload = {
        "schema_version": 2,
        "source": "github_actions",
        "run_id": run.run_id,
        "mode": run.mode.value,
        "status": status,
        "scored_at": run.scored_at.isoformat(),
        "model_endpoint": model_endpoint,
        "source_revision": source_revision,
        "issue_number": classification.issue_number,
        "content_hash": classification.content_hash,
        "classification": {
            "type": classification.issue_type.label,
            "impact": classification.impact.value,
            "area_keys": list(classification.area_keys),
            "component_labels": list(classification.component_labels),
            "reasoning": classification.reasoning,
        },
        "score": _score_payload(decision),
        "mutation": _mutation_payload(plan),
        "comment": {
            "body": build_triage_comment(
                decision,
                plan,
                labels_after if labels_after is not None else _planned_labels_after(decision, plan),
            )
        },
        "applied_bot_state": (
            _bot_state_payload(applied_bot_state) if applied_bot_state is not None else None
        ),
        "labels_before": list(labels_before),
        "labels_after": list(labels_after) if labels_after is not None else None,
    }
    (output_dir / "event.json").write_text(json.dumps(payload, indent=2) + "\n")
    (output_dir / "mutations.json").write_text(
        json.dumps([_mutation_payload(plan)], indent=2) + "\n"
    )


def _score_payload(item: RankedIssue) -> dict[str, object]:
    issue = item.issue
    result = item.result
    return {
        "title": issue.title,
        "url": issue.url,
        "type": issue.issue_type.label,
        "impact": issue.impact.value,
        "score": float(result.score),
        "current_priority": issue.current_priority.value if issue.current_priority else None,
        "proposed_priority": result.priority.value,
        "area_keys": list(issue.area_keys),
        "component_labels": list(issue.component_labels),
        "duplicate_count": issue.duplicate_count,
        "upvote_count": issue.upvote_count,
        "breakdown": [
            {
                "name": step.name,
                "operation": step.operation,
                "value": float(step.value),
                "score_before": float(step.score_before),
                "score_after": float(step.score_after),
            }
            for step in result.steps
        ],
    }


def _mutation_payload(plan: MutationPlan) -> dict[str, object]:
    return {
        "issue_number": plan.target.issue_number,
        "target": {
            "priority": plan.target.priority,
            "components": list(plan.target.components),
        },
        "labels_add": list(plan.labels_add),
        "labels_remove": list(plan.labels_remove),
        "blocked": list(plan.blocked),
        "next_bot_state": _bot_state_payload(plan.next_state),
    }


def _bot_state_payload(state: BotState) -> dict[str, object]:
    return {
        "priority": state.priority,
        "components": list(state.components),
    }


def _write_skip_artifact(output_dir: Path, run_id: str, issue_number: int, reason: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "source": "github_actions",
        "run_id": run_id,
        "issue_number": issue_number,
        "status": "skipped",
        "reason": reason,
    }
    (output_dir / "event.json").write_text(json.dumps(payload, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prioritize one newly opened issue")
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--github-repo", required=True)
    parser.add_argument("--model-endpoint", required=True)
    parser.add_argument("--areas", required=True, type=Path)
    parser.add_argument("--label-manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-revision", default="")
    parser.add_argument("--mode", choices=list(PipelineMode), default=PipelineMode.DRY_RUN)
    args = parser.parse_args()
    if args.issue_number <= 0:
        raise ValueError("issue_number must be positive")

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required")
    client = GitHubClient(token, args.github_repo)
    issue = client.open_issue(args.issue_number)
    if issue is None:
        _write_skip_artifact(args.output_dir, args.run_id, args.issue_number, "issue_not_open")
        print(f"Skipping #{args.issue_number}: issue is not open")
        return
    config = ScoringConfig.default()
    areas = AreaCatalog.from_json(args.areas)
    manifest = LabelManifest.from_json(args.label_manifest)
    mode = PipelineMode(args.mode)
    run, classification, planner, states = prioritize_issue(
        issue,
        serving_endpoint_classifier(args.model_endpoint, areas),
        config,
        areas,
        manifest,
        args.run_id,
        mode,
    )
    write_event_artifacts(
        args.output_dir,
        run,
        classification,
        config,
        args.model_endpoint,
        args.source_revision,
        issue.labels,
    )
    decision = run.ranked[0]
    if mode == PipelineMode.APPLY:
        engine = ScoreEngine(config, areas)

        def resolve_target(
            _: MutationTarget,
            current_labels: tuple[str, ...],
            state: BotState | None,
        ) -> MutationTarget:
            return target_for_labels(
                issue,
                classification,
                run.scored_at,
                current_labels,
                engine,
            )

        applied_plans: tuple[MutationPlan, ...] = ()
        try:
            applied_plans = GitHubMutationSink(
                client,
                manifest,
                planner,
                states,
                target_resolver=resolve_target,
            ).apply_with_plans(run)
            if len(applied_plans) != 1:
                raise RuntimeError("targeted apply must produce exactly one mutation plan")
            labels_after = client.issue_labels(issue.number)
        except Exception:
            write_event_status(
                args.output_dir,
                run,
                classification,
                args.model_endpoint,
                args.source_revision,
                issue.labels,
                status="apply_unknown",
                plan=applied_plans[0] if applied_plans else None,
                applied_bot_state=states.load().get(issue.number),
            )
            raise
        decision = _rank_issue(
            issue,
            classification,
            run.scored_at,
            labels_after,
            engine,
        )
        write_event_status(
            args.output_dir,
            run,
            classification,
            args.model_endpoint,
            args.source_revision,
            issue.labels,
            status="applied",
            labels_after=labels_after,
            plan=applied_plans[0],
            decision=decision,
            applied_bot_state=states.load().get(issue.number),
        )
    print(
        f"Issue #{issue.number}: impact={decision.issue.impact.value}, "
        f"score={decision.result.score}, priority={decision.result.priority.value}, "
        f"mode={mode.value}"
    )


def _planned_labels_after(item: RankedIssue, plan: MutationPlan) -> tuple[str, ...]:
    current_priority = item.issue.current_priority
    labels = {current_priority.value} if current_priority else set()
    labels = (labels - set(plan.labels_remove)) | set(plan.labels_add)
    return tuple(sorted(labels))


if __name__ == "__main__":
    main()
