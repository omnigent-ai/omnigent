"""The Archived view explains when archive cleanup keeps a worktree."""

from __future__ import annotations

import contextlib
import json
import uuid
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle

_KEPT_LABEL = {
    "dirty_files": 2,
    "unpushed_commits": 1,
    "merged": False,
    "default_ref": "origin/main",
}


def _seed_archived_session(base_url: str, *, title: str) -> str:
    create_response = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create_response.raise_for_status()
    session_id = create_response.json()["session_id"]
    archive_response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title, "archived": True},
        timeout=10.0,
    )
    archive_response.raise_for_status()
    return session_id


def _inject_kept_label(page: Page, session_id: str) -> None:
    def _patch_list(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/v1/sessions":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("id") == session_id:
                    labels = dict(row.get("labels") or {})
                    labels["omnigent.worktree_kept"] = json.dumps(_KEPT_LABEL)
                    row["labels"] = labels
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions**", _patch_list)


def test_archived_row_shows_worktree_kept_note(live_server: str, page: Page) -> None:
    suffix = uuid.uuid4().hex[:8]
    kept_title = f"kept-worktree-{suffix}"
    plain_title = f"plain-archived-{suffix}"
    kept_id = _seed_archived_session(live_server, title=kept_title)
    plain_id = _seed_archived_session(live_server, title=plain_title)
    _inject_kept_label(page, kept_id)
    try:
        page.goto(f"{live_server}/settings/archived")

        kept_row = page.locator("li[data-testid='archived-row']", has_text=kept_title)
        expect(kept_row.get_by_test_id("worktree-kept-note")).to_have_text(
            "Worktree kept — 2 uncommitted changes, 1 unpushed commit, "
            "branch not merged into origin/main."
        )
        plain_row = page.locator("li[data-testid='archived-row']", has_text=plain_title)
        expect(plain_row.get_by_test_id("worktree-kept-note")).to_have_count(0)
        page.wait_for_timeout(4_500)
    finally:
        for session_id in (kept_id, plain_id):
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
