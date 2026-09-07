from __future__ import annotations

import argparse
import copy
import json
from collections.abc import Mapping
from pathlib import Path

DATASET_NAME = "issue_priority_ranking"
PAGE_NAME = "issue_analysis"
WIDGET_NAME = "ia-priority-ranking"


def patch_dashboard(value: Mapping[str, object]) -> dict[str, object]:
    dashboard = _serialized_dashboard(value)
    datasets = dashboard.get("datasets")
    pages = dashboard.get("pages")
    if not isinstance(datasets, list) or not isinstance(pages, list):
        raise ValueError("dashboard must contain datasets and pages")

    replacement = _ranking_dataset()
    dashboard["datasets"] = [
        *[dataset for dataset in datasets if _name(dataset) != DATASET_NAME],
        replacement,
    ]

    page = next((item for item in pages if _name(item) == PAGE_NAME), None)
    if not isinstance(page, dict):
        raise ValueError(f"dashboard page {PAGE_NAME!r} not found")
    layout = page.get("layout")
    if not isinstance(layout, list):
        raise ValueError(f"dashboard page {PAGE_NAME!r} has no layout")

    retained = [item for item in layout if _widget_name(item) != WIDGET_NAME]
    page["layout"] = [*retained, _ranking_widget(_next_row(retained))]
    return dashboard


def _serialized_dashboard(value: Mapping[str, object]) -> dict[str, object]:
    serialized = value.get("serialized_dashboard")
    if isinstance(serialized, str):
        parsed = json.loads(serialized)
        if not isinstance(parsed, dict):
            raise ValueError("serialized_dashboard must contain a JSON object")
        return parsed
    return copy.deepcopy(dict(value))


def _name(value: object) -> object:
    return value.get("name") if isinstance(value, Mapping) else None


def _widget_name(value: object) -> object:
    if not isinstance(value, Mapping):
        return None
    return _name(value.get("widget"))


def _next_row(layout: list[object]) -> int:
    bottoms = []
    for item in layout:
        if not isinstance(item, Mapping):
            continue
        position = item.get("position")
        if not isinstance(position, Mapping):
            continue
        bottoms.append(int(position.get("y", 0)) + int(position.get("height", 0)))
    return max(bottoms, default=0)


def _ranking_dataset() -> dict[str, object]:
    return {
        "name": DATASET_NAME,
        "displayName": "Issue Priority Ranking",
        "queryLines": [
            "SELECT\n",
            "  rank,\n",
            "  score,\n",
            "  proposed_priority,\n",
            "  COALESCE(current_priority, 'Unprioritized') AS current_priority,\n",
            "  impact,\n",
            "  issue_number,\n",
            "  title,\n",
            "  CONCAT_WS(', ', component_labels) AS components,\n",
            "  upvote_count,\n",
            "  CONCAT_WS(', ', mutation_blocked) AS mutation_blocked,\n",
            "  url\n",
            "FROM main.team_eng_omnigent.issue_scores_latest\n",
            "ORDER BY rank ",
        ],
    }


def _ranking_widget(y: int) -> dict[str, object]:
    fields = [
        "rank",
        "score",
        "proposed_priority",
        "current_priority",
        "impact",
        "issue_number",
        "title",
        "components",
        "upvote_count",
        "mutation_blocked",
        "url",
    ]
    columns: list[dict[str, object]] = [
        {"fieldName": "rank", "displayName": "Rank"},
        {
            "fieldName": "score",
            "displayName": "Score",
            "format": {
                "type": "number",
                "decimalPlaces": {"type": "max", "places": 2},
            },
        },
        {"fieldName": "proposed_priority", "displayName": "Proposed"},
        {"fieldName": "current_priority", "displayName": "Current"},
        {"fieldName": "impact", "displayName": "Impact"},
        {
            "fieldName": "issue_number",
            "displayName": "Issue",
            "link": {"templatedURL": "{{url}}"},
        },
        {"fieldName": "title", "displayName": "Title"},
        {"fieldName": "components", "displayName": "Components"},
        {"fieldName": "upvote_count", "displayName": "Upvotes"},
        {"fieldName": "mutation_blocked", "displayName": "Protected Overrides"},
    ]
    return {
        "widget": {
            "name": WIDGET_NAME,
            "queries": [
                {
                    "name": "main_query",
                    "query": {
                        "datasetName": DATASET_NAME,
                        "fields": [{"name": field, "expression": f"`{field}`"} for field in fields],
                        "disaggregated": True,
                    },
                }
            ],
            "spec": {
                "version": 2,
                "widgetType": "table",
                "frame": {
                    "showTitle": True,
                    "title": "Issue Priority Ranking",
                    "showDescription": True,
                    "description": (
                        "All issues from the latest complete scoring run. Proposed labels "
                        "remain a dry-run until GitHub writes are explicitly enabled."
                    ),
                },
                "encodings": {"columns": columns},
                "data": {"queryName": "main_query"},
            },
        },
        "position": {"x": 0, "y": y, "width": 12, "height": 8},
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a local issue-ranking patch for an Omnigent dashboard export."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    source = json.loads(args.input.read_text())
    if not isinstance(source, dict):
        raise ValueError("dashboard input must be a JSON object")
    args.output.write_text(json.dumps(patch_dashboard(source), indent=2) + "\n")
