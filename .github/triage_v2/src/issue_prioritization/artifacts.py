from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from issue_prioritization.config import ScoringConfig
from issue_prioritization.domain import Issue, Priority, ScoreResult
from issue_prioritization.scoring import ScoreEngine


@dataclass(frozen=True)
class RankedIssue:
    rank: int
    previous_rank: int
    issue: Issue
    result: ScoreResult

    @property
    def rank_delta(self) -> int:
        return self.previous_rank - self.rank


def rank_issues(issues: list[Issue], engine: ScoreEngine) -> list[RankedIssue]:
    current = sorted(issues, key=_current_rank_key)
    previous_rank = {issue.number: rank for rank, issue in enumerate(current, start=1)}
    scored = [(issue, engine.score(issue)) for issue in issues]
    scored.sort(key=lambda item: (item[1].score, item[0].number), reverse=True)
    return [
        RankedIssue(rank, previous_rank[issue.number], issue, result)
        for rank, (issue, result) in enumerate(scored, start=1)
    ]


def write_artifacts(
    output_dir: str | Path,
    ranked: list[RankedIssue],
    config: ScoringConfig,
) -> None:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    rows = [_row(item) for item in ranked]
    config_payload = config.as_dict()
    config_json = json.dumps(config_payload, sort_keys=True, separators=(",", ":"))
    summary = {
        "issue_count": len(rows),
        "config_sha256": hashlib.sha256(config_json.encode()).hexdigest(),
        "priority_counts": dict(Counter(row["proposed_priority"] for row in rows)),
        "priority_changes": sum(
            row["current_priority"] != row["proposed_priority"]
            for row in rows
            if row["current_priority"]
        ),
    }
    (destination / "ranking.json").write_text(json.dumps(rows, indent=2) + "\n")
    (destination / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (destination / "config.json").write_text(json.dumps(config_payload, indent=2) + "\n")
    _write_csv(destination / "ranking.csv", rows)
    _write_markdown(destination / "ranking.md", rows)


def _current_rank_key(issue: Issue) -> tuple[int, int]:
    order = {Priority.P0: 0, Priority.P1: 1, Priority.P2: 2, Priority.P3: 3, None: 4}
    return order[issue.current_priority], -issue.number


def _row(item: RankedIssue) -> dict[str, object]:
    issue = item.issue
    result = item.result
    return {
        "rank": item.rank,
        "previous_rank": item.previous_rank,
        "rank_delta": item.rank_delta,
        "issue_number": issue.number,
        "title": issue.title,
        "url": issue.url,
        "type": issue.issue_type.label,
        "impact": issue.impact.value,
        "classification_reasoning": issue.classification_reasoning,
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


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = [
        "rank",
        "previous_rank",
        "rank_delta",
        "issue_number",
        "title",
        "url",
        "type",
        "impact",
        "score",
        "current_priority",
        "proposed_priority",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_markdown(path: Path, rows: list[dict[str, object]]) -> None:
    lines = [
        "| Rank | Score | Impact | Current | Proposed | Δrank | Issue |",
        "|---:|---:|---|---|---|---:|---|",
    ]
    for row in rows:
        title = str(row["title"]).replace("|", "\\|")
        issue = f"[#{row['issue_number']}]({row['url']}) {title}"
        lines.append(
            f"| {row['rank']} | {row['score']:.2f} | {row['impact']} | "
            f"{row['current_priority'] or 'none'} | {row['proposed_priority']} | "
            f"{row['rank_delta']:+d} | {issue} |"
        )
    path.write_text("\n".join(lines) + "\n")
