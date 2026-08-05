from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from issue_prioritization.artifacts import RankedIssue, rank_issues
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.classification import Classification, Classifier
from issue_prioritization.scoring import ScoreEngine


class PipelineMode(StrEnum):
    DRY_RUN = "dry_run"


class IssueSource(Protocol):
    def load_open_issues(self) -> list[BronzeIssue]: ...


class ClassificationRepository(Protocol):
    def load(self) -> dict[int, Classification]: ...

    def upsert(self, classifications: list[Classification]) -> None: ...


class ScoreSink(Protocol):
    def write(self, run: PipelineRun) -> None: ...


class ArtifactSink(Protocol):
    def write(self, run: PipelineRun) -> None: ...


@dataclass(frozen=True)
class PipelineRun:
    run_id: str
    mode: PipelineMode
    scored_at: datetime
    ranked: tuple[RankedIssue, ...]
    classifications_updated: int


class IssuePrioritizationPipeline:
    def __init__(
        self,
        source: IssueSource,
        classifier: Classifier,
        classifications: ClassificationRepository,
        scores: ScoreSink,
        artifacts: ArtifactSink,
        engine: ScoreEngine,
        maintainers: set[str],
    ) -> None:
        self.source = source
        self.classifier = classifier
        self.classifications = classifications
        self.scores = scores
        self.artifacts = artifacts
        self.engine = engine
        self.maintainers = maintainers

    def run(self, run_id: str, mode: PipelineMode = PipelineMode.DRY_RUN) -> PipelineRun:
        now = datetime.now(UTC)
        issues = [
            issue
            for issue in self.source.load_open_issues()
            if issue.author.lower() not in self.maintainers
        ]
        existing = self.classifications.load()
        resolved: dict[int, Classification] = {}
        updated = []
        for issue in issues:
            cached = existing.get(issue.number)
            if cached and cached.content_hash == issue.content().content_hash:
                resolved[issue.number] = cached
                continue
            classification = self.classifier.classify(issue.content())
            resolved[issue.number] = classification
            updated.append(classification)
        if updated:
            self.classifications.upsert(updated)

        normalized = [issue.to_issue(resolved[issue.number], now) for issue in issues]
        run = PipelineRun(
            run_id=run_id,
            mode=mode,
            scored_at=now,
            ranked=tuple(rank_issues(normalized, self.engine)),
            classifications_updated=len(updated),
        )
        self.scores.write(run)
        self.artifacts.write(run)
        return run
