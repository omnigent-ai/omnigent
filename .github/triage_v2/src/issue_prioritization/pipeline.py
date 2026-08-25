from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from issue_prioritization.artifacts import RankedIssue, rank_issues
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.classification import Classification, Classifier
from issue_prioritization.mutations import MutationPlan, MutationPlanner
from issue_prioritization.scoring import ScoreEngine


class PipelineMode(StrEnum):
    DRY_RUN = "dry_run"
    APPLY = "apply"


class IssueSource(Protocol):
    def load_open_issues(self) -> list[BronzeIssue]: ...


class ClassificationRepository(Protocol):
    def load(self) -> dict[int, Classification]: ...

    def upsert(self, classifications: list[Classification]) -> None: ...


class ScoreSink(Protocol):
    def write(self, run: PipelineRun) -> None: ...


class ArtifactSink(Protocol):
    def write(self, run: PipelineRun) -> None: ...


class MutationSink(Protocol):
    def apply(self, run: PipelineRun) -> None: ...


@dataclass(frozen=True)
class PipelineRun:
    run_id: str
    mode: PipelineMode
    scored_at: datetime
    ranked: tuple[RankedIssue, ...]
    classifications_updated: int
    mutations: tuple[MutationPlan, ...]
    regrade: bool = False
    adopt_legacy_bot_priorities: bool = False
    legacy_priorities_adopted: int = 0


class IssuePrioritizationPipeline:
    def __init__(
        self,
        source: IssueSource,
        classifier: Classifier,
        classifications: ClassificationRepository,
        scores: ScoreSink,
        artifacts: ArtifactSink,
        engine: ScoreEngine,
        mutation_planner: MutationPlanner | None = None,
        mutation_sink: MutationSink | None = None,
        classification_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        self.source = source
        self.classifier = classifier
        self.classifications = classifications
        self.scores = scores
        self.artifacts = artifacts
        self.engine = engine
        self.mutation_planner = mutation_planner
        self.mutation_sink = mutation_sink
        self.classification_progress = classification_progress

    def run(
        self,
        run_id: str,
        mode: PipelineMode = PipelineMode.DRY_RUN,
        regrade: bool = False,
        adopt_legacy_bot_priorities: bool = False,
    ) -> PipelineRun:
        now = datetime.now(UTC)
        issues = self.source.load_open_issues()
        existing = self.classifications.load()
        contents = {issue.number: issue.content() for issue in issues}
        refresh = {
            issue.number
            for issue in issues
            if regrade
            or not (cached := existing.get(issue.number))
            or cached.content_hash != contents[issue.number].content_hash
        }
        if self.classification_progress:
            self.classification_progress(0, len(refresh))
        resolved: dict[int, Classification] = {}
        updated = []
        for issue in issues:
            cached = existing.get(issue.number)
            if issue.number not in refresh and cached:
                resolved[issue.number] = cached
                continue
            classification = self.classifier.classify(contents[issue.number])
            resolved[issue.number] = classification
            updated.append(classification)
            if self.classification_progress:
                self.classification_progress(len(updated), len(refresh))
        if updated:
            self.classifications.upsert(updated)

        persisted_bot_states = self.mutation_planner.load_states() if self.mutation_planner else {}
        bot_states = persisted_bot_states
        if self.mutation_planner:
            bot_states = {
                issue.number: state
                for issue in issues
                if (
                    state := self.mutation_planner.resolve_state(
                        issue.number,
                        issue.labels,
                        bot_states.get(issue.number),
                    )
                )
                is not None
            }
        normalized = []
        for issue in issues:
            normalized_issue = issue.to_issue(resolved[issue.number], now)
            normalized.append(normalized_issue)
        ranked = tuple(rank_issues(normalized, self.engine))
        current_labels = {issue.number: issue.labels for issue in issues}
        mutations = (
            self.mutation_planner.plan_all(ranked, current_labels, bot_states)
            if self.mutation_planner
            else ()
        )
        run = PipelineRun(
            run_id=run_id,
            mode=mode,
            scored_at=now,
            ranked=ranked,
            classifications_updated=len(updated),
            mutations=mutations,
            regrade=regrade,
            adopt_legacy_bot_priorities=adopt_legacy_bot_priorities,
            legacy_priorities_adopted=len(set(bot_states) - set(persisted_bot_states)),
        )
        self.artifacts.write(run)
        self.scores.write(run)
        if mode == PipelineMode.APPLY:
            if self.mutation_sink is None:
                raise RuntimeError("apply mode requires a mutation sink")
            self.mutation_sink.apply(run)
        return run
