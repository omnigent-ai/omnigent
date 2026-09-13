from __future__ import annotations

import argparse
import json
from pathlib import Path

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.artifacts import rank_issues, write_artifacts
from issue_prioritization.config import ScoringConfig
from issue_prioritization.domain import Issue
from issue_prioritization.scoring import ScoreEngine


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate issue-prioritization dry-run artifacts")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--areas", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    raw = json.loads(args.input.read_text())
    raw_issues = raw["issues"] if isinstance(raw, dict) else raw
    if not isinstance(raw_issues, list):
        raise ValueError("input must be an array or an object with an issues array")

    issues = [Issue.from_mapping(value) for value in raw_issues]
    config = ScoringConfig.from_json(args.config) if args.config else ScoringConfig.default()
    engine = ScoreEngine(config, AreaCatalog.from_json(args.areas))
    ranked = rank_issues(issues, engine)
    write_artifacts(args.output_dir, ranked, config)
    print(f"Wrote {len(ranked)} ranked issues to {args.output_dir}")


if __name__ == "__main__":
    main()
