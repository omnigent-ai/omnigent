from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from issue_prioritization.areas import Area, AreaCatalog
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.classification import Classification
from issue_prioritization.config import ScoringConfig
from issue_prioritization.domain import IssueType, Severity
from issue_prioritization.labels import LabelDefinition, LabelManifest
from issue_prioritization.mutations import MutationPlanner
from issue_prioritization.pipeline import IssuePrioritizationPipeline
from issue_prioritization.scoring import ScoreEngine


class FakeSource:
    def __init__(self, issues):
        self.issues = issues

    def load_open_issues(self):
        return self.issues


class FakeClassifier:
    def __init__(self, classification):
        self.classification = classification
        self.calls = 0

    def classify(self, issue):
        self.calls += 1
        return self.classification


class FakeClassifications:
    def __init__(self, values):
        self.values = values
        self.updated = []

    def load(self):
        return self.values

    def upsert(self, classifications):
        self.updated.extend(classifications)


class CaptureSink:
    def __init__(self):
        self.runs = []

    def write(self, run):
        self.runs.append(run)


class FakeStates:
    def load(self):
        return {}

    def upsert(self, states):
        pass


class FakeLegacyPriorities:
    def is_bot_owned(self, issue_number, priority):
        return True


def _bronze(number, author="community"):
    return BronzeIssue(
        number=number,
        title="Database fails",
        body="Cannot start",
        url=f"https://github.com/omnigent-ai/omnigent/issues/{number}",
        author=author,
        labels=("Bug", "P2-medium"),
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        reaction_count=0,
        duplicate_count=0,
    )


def test_pipeline_reuses_persisted_classification_and_excludes_maintainers() -> None:
    issue = _bronze(1)
    classification = Classification(
        issue_number=1,
        issue_type=IssueType.BUG,
        severity=Severity.S1,
        area_keys=("db",),
        component_labels=("comp:db",),
        reasoning="No workaround",
        content_hash=issue.content().content_hash,
    )
    classifier = FakeClassifier(classification)
    classifications = FakeClassifications({1: classification})
    scores = CaptureSink()
    artifacts = CaptureSink()
    area = Area("db", "comp:db", Decimal("1.2"))
    catalog = AreaCatalog(by_key={"db": area}, by_label={"comp:db": (area,)})
    pipeline = IssuePrioritizationPipeline(
        source=FakeSource([issue, _bronze(2, author="maintainer")]),
        classifier=classifier,
        classifications=classifications,
        scores=scores,
        artifacts=artifacts,
        engine=ScoreEngine(ScoringConfig.default(), catalog),
        maintainers={"maintainer"},
    )

    run = pipeline.run("run-1")

    assert classifier.calls == 0
    assert classifications.updated == []
    assert len(run.ranked) == 1
    assert run.ranked[0].result.score == Decimal("72.00")
    assert scores.runs == [run]
    assert artifacts.runs == [run]


def test_pipeline_reclassifies_changed_content() -> None:
    issue = _bronze(1)
    classification = Classification(
        issue_number=1,
        issue_type=IssueType.BUG,
        severity=Severity.S2,
        area_keys=("db",),
        component_labels=("comp:db",),
        reasoning="Has mitigation",
        content_hash=issue.content().content_hash,
    )
    stale = Classification(
        issue_number=1,
        issue_type=IssueType.BUG,
        severity=Severity.S3,
        area_keys=("db",),
        component_labels=("comp:db",),
        reasoning="Old",
        content_hash="old",
    )
    classifier = FakeClassifier(classification)
    classifications = FakeClassifications({1: stale})
    sink = CaptureSink()
    area = Area("db", "comp:db", Decimal("1.2"))
    catalog = AreaCatalog(by_key={"db": area}, by_label={"comp:db": (area,)})
    pipeline = IssuePrioritizationPipeline(
        source=FakeSource([issue]),
        classifier=classifier,
        classifications=classifications,
        scores=sink,
        artifacts=sink,
        engine=ScoreEngine(ScoringConfig.default(), catalog),
        maintainers=set(),
    )

    run = pipeline.run("run-2")

    assert classifier.calls == 1
    assert classifications.updated == [classification]
    assert run.classifications_updated == 1


def test_pipeline_can_force_regrade_cached_content() -> None:
    issue = _bronze(1)
    classification = Classification(
        issue_number=1,
        issue_type=IssueType.BUG,
        severity=Severity.S2,
        area_keys=("db",),
        component_labels=("comp:db",),
        reasoning="Refreshed",
        content_hash=issue.content().content_hash,
    )
    classifier = FakeClassifier(classification)
    classifications = FakeClassifications({1: classification})
    sink = CaptureSink()
    area = Area("db", "comp:db", Decimal("1.2"))
    catalog = AreaCatalog(by_key={"db": area}, by_label={"comp:db": (area,)})
    pipeline = IssuePrioritizationPipeline(
        source=FakeSource([issue]),
        classifier=classifier,
        classifications=classifications,
        scores=sink,
        artifacts=sink,
        engine=ScoreEngine(ScoringConfig.default(), catalog),
        maintainers=set(),
    )

    pipeline.run("run-regrade", regrade=True)

    assert classifier.calls == 1
    assert classifications.updated == [classification]


def test_pipeline_scores_with_human_severity_override() -> None:
    issue = _bronze(1)
    issue = replace(issue, labels=(*issue.labels, "severity:S3"))
    classification = Classification(
        issue_number=1,
        issue_type=IssueType.BUG,
        severity=Severity.S1,
        area_keys=("db",),
        component_labels=("comp:db",),
        reasoning="No workaround",
        content_hash=issue.content().content_hash,
    )
    area = Area("db", "comp:db", Decimal("1.2"))
    catalog = AreaCatalog(by_key={"db": area}, by_label={"comp:db": (area,)})
    manifest = LabelManifest(labels=(LabelDefinition("severity:S3", "000000", ""),))
    pipeline = IssuePrioritizationPipeline(
        source=FakeSource([issue]),
        classifier=FakeClassifier(classification),
        classifications=FakeClassifications({1: classification}),
        scores=CaptureSink(),
        artifacts=CaptureSink(),
        engine=ScoreEngine(ScoringConfig.default(), catalog),
        maintainers=set(),
        mutation_planner=MutationPlanner(manifest, FakeStates()),
    )

    run = pipeline.run("run-human-severity")

    assert run.ranked[0].issue.severity == Severity.S3
    assert run.ranked[0].result.score == Decimal("12.00")


def test_dry_run_previews_safe_legacy_priority_regrade() -> None:
    issue = _bronze(1)
    classification = Classification(
        issue_number=1,
        issue_type=IssueType.BUG,
        severity=Severity.S1,
        area_keys=("db",),
        component_labels=("comp:db",),
        reasoning="No workaround",
        content_hash=issue.content().content_hash,
    )
    area = Area("db", "comp:db", Decimal("1.2"))
    catalog = AreaCatalog(by_key={"db": area}, by_label={"comp:db": (area,)})
    manifest = LabelManifest(
        labels=(
            LabelDefinition("severity:S1", "000000", ""),
            LabelDefinition("comp:db", "000000", ""),
        )
    )
    planner = MutationPlanner(
        manifest,
        FakeStates(),
        FakeLegacyPriorities(),
    )
    pipeline = IssuePrioritizationPipeline(
        source=FakeSource([issue]),
        classifier=FakeClassifier(classification),
        classifications=FakeClassifications({1: classification}),
        scores=CaptureSink(),
        artifacts=CaptureSink(),
        engine=ScoreEngine(ScoringConfig.default(), catalog),
        maintainers=set(),
        mutation_planner=planner,
    )

    run = pipeline.run(
        "run-legacy-preview",
        adopt_legacy_bot_priorities=True,
    )

    assert run.legacy_priorities_adopted == 1
    assert set(run.mutations[0].labels_add) == {"P1-high", "comp:db", "severity:S1"}
    assert run.mutations[0].labels_remove == ("P2-medium",)
