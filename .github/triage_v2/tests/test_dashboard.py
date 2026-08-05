from __future__ import annotations

import json

import pytest

from issue_prioritization.dashboard import DATASET_NAME, WIDGET_NAME, patch_dashboard


def _dashboard() -> dict[str, object]:
    return {
        "datasets": [{"name": "existing", "queryLines": ["SELECT 1 "]}],
        "pages": [
            {
                "name": "issue_analysis",
                "pageType": "PAGE_TYPE_CANVAS",
                "layoutVersion": "GRID_V1",
                "layout": [
                    {
                        "widget": {"name": "existing-widget"},
                        "position": {"x": 0, "y": 5, "width": 12, "height": 7},
                    }
                ],
            }
        ],
    }


def test_dashboard_patch_adds_ranking_after_existing_layout() -> None:
    patched = patch_dashboard(_dashboard())

    dataset = next(item for item in patched["datasets"] if item["name"] == DATASET_NAME)
    assert "issue_scores_latest" in "".join(dataset["queryLines"])
    assert "LIMIT" not in "".join(dataset["queryLines"])
    assert dataset["queryLines"][-1].endswith(" ")

    widget = patched["pages"][0]["layout"][-1]
    assert widget["widget"]["name"] == WIDGET_NAME
    assert widget["position"] == {"x": 0, "y": 12, "width": 12, "height": 8}
    assert widget["widget"]["spec"]["version"] == 2
    assert widget["widget"]["spec"]["widgetType"] == "table"
    fields = {item["name"] for item in widget["widget"]["queries"][0]["query"]["fields"]}
    columns = {item["fieldName"] for item in widget["widget"]["spec"]["encodings"]["columns"]}
    assert columns <= fields


def test_dashboard_patch_accepts_rest_response_and_is_idempotent() -> None:
    response = {"serialized_dashboard": json.dumps(_dashboard())}

    once = patch_dashboard(response)
    twice = patch_dashboard(once)

    assert twice == once
    assert sum(item["name"] == DATASET_NAME for item in twice["datasets"]) == 1
    assert sum(item["widget"]["name"] == WIDGET_NAME for item in twice["pages"][0]["layout"]) == 1


def test_dashboard_patch_requires_issue_analysis_page() -> None:
    with pytest.raises(ValueError, match="issue_analysis"):
        patch_dashboard({"datasets": [], "pages": []})
