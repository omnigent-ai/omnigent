from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal

from issue_prioritization.areas import Area, AreaCatalog
from issue_prioritization.artifacts import rank_issues, write_artifacts
from issue_prioritization.config import ScoringConfig
from issue_prioritization.domain import Impact, Issue, IssueType, Priority
from issue_prioritization.scoring import ScoreEngine


def test_dry_run_artifacts_are_complete_and_deterministic(tmp_path) -> None:
    area = Area("db", "comp:server", Decimal("1.2"))
    catalog = AreaCatalog(by_key={"db": area}, by_label={"comp:server": (area,)})
    issues = [
        Issue(
            number=2,
            title="Database crash",
            url="https://github.com/omnigent-ai/omnigent/issues/2",
            issue_type=IssueType.BUG,
            impact=Impact.HIGH,
            area_keys=("db",),
            current_priority=Priority.P2,
            upvote_count=3,
            duplicate_count=2,
        ),
        Issue(
            number=1,
            title="Small request",
            url="https://github.com/omnigent-ai/omnigent/issues/1",
            issue_type=IssueType.ENHANCEMENT,
            impact=Impact.LOW,
            area_keys=("db",),
            current_priority=Priority.P1,
        ),
    ]
    config = ScoringConfig.default()
    ranked = rank_issues(issues, ScoreEngine(config, catalog))

    first = tmp_path / "first"
    second = tmp_path / "second"
    write_artifacts(first, ranked, config)
    write_artifacts(second, ranked, config)

    expected = {"ranking.json", "ranking.csv", "ranking.md", "summary.json", "config.json"}
    assert {path.name for path in first.iterdir()} == expected
    assert (first / "ranking.json").read_bytes() == (second / "ranking.json").read_bytes()
    summary = json.loads((first / "summary.json").read_text())
    assert summary["issue_count"] == 2
    assert summary["priority_changes"] == 2
    ranking = json.loads((first / "ranking.json").read_text())
    assert ranking[0]["upvote_count"] == 3
    assert ranking[0]["duplicate_count"] == 2
    assert ranking[1]["type"] == "Feature"
    assert ranking[0]["impact"] == "high"


def test_cli_writes_review_artifacts_without_network(tmp_path) -> None:
    issues_path = tmp_path / "issues.json"
    areas_path = tmp_path / "areas.json"
    output_path = tmp_path / "output"
    issues_path.write_text(
        json.dumps(
            [
                {
                    "number": 7,
                    "title": "iOS login fails",
                    "url": "https://github.com/omnigent-ai/omnigent/issues/7",
                    "type": "Bug",
                    "severity": "S1",
                    "area_keys": ["ios"],
                    "current_priority": "P2-medium",
                }
            ]
        )
    )
    areas_path.write_text(
        json.dumps(
            {
                "areas": [
                    {
                        "key": "ios",
                        "label": "comp:ios",
                        "weight": 1.0,
                    }
                ]
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "issue_prioritization.cli",
            "--input",
            str(issues_path),
            "--areas",
            str(areas_path),
            "--output-dir",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Wrote 1 ranked issues" in result.stdout
    assert (
        json.loads((output_path / "ranking.json").read_text())[0]["proposed_priority"] == "P1-high"
    )
