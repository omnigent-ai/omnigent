"""E2E: the work-items REST round-trips on the live server (#3).

Creates a work item via ``POST /v1/work-items`` and reads it back from the
listing — proving the store + routes are wired end to end. The agent-tool path
(create_work_item etc.) is unit-covered in
``tests/runner/test_work_management_dispatch.py``.
"""

from __future__ import annotations

import uuid

import httpx


def test_work_item_create_then_list(http_client: httpx.Client) -> None:
    title = f"e2e-task-{uuid.uuid4().hex[:8]}"
    created = http_client.post("/v1/work-items", json={"title": title, "source": "manual"})
    created.raise_for_status()
    assert created.json()["title"] == title

    items = http_client.get("/v1/work-items").json()["data"]
    assert any(w["title"] == title for w in items), (
        f"{title!r} not in {[w['title'] for w in items]}"
    )
