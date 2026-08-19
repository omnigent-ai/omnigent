"""E2E: the Archived view explains when an archived session's worktree was kept.

Archiving a worktree-bound session removes the worktree only when the host
verifies nothing would be lost; otherwise the directory stays on disk and the
server records why on the session's ``omnigent.worktree_kept`` label. The
Archived settings view surfaces that label as a "Worktree kept — …" note on
the row, so the user isn't left thinking the disk was cleaned when it wasn't.

The label is server-internal (the PATCH route rejects it from clients), and
reproducing a real host + dirty worktree end-to-end would need a live host
tunnel — so, like ``test_host_badge.py``, this patches the browser's view
instead: seed and archive two plain sessions over REST, then inject the label
into one row's ``GET /v1/sessions`` payload via route interception.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle

_KEPT_LABEL = {
    "dirty_files": 2,
    "unpushed_commits": 1,
    "merged": False,
    "default_ref": "origin/main",
}


@pytest.fixture(autouse=True)
def _drop_routes(page: Page) -> Iterator[None]:
    """Drop this module's route handlers before the page closes.

    :param page: Playwright page fixture.
    :returns: Iterator yielding once, then unrouting.
    """
    yield
    page.unroute_all(behavior="ignoreErrors")


def _seed_archived_session(base_url: str, *, title: str) -> str:
    """Create a hello_world session and archive it.

    Plain (non-worktree) sessions archive without any cleanup frames, so the
    server sets no kept-label itself — the test injects one over interception.

    :param base_url: The live server base URL.
    :param title: Unique title so the row is easy to spot on the shared server.
    :returns: The new session id.
    """
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title, "archived": True},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _inject_kept_label(page: Page, session_id: str) -> None:
    """Patch ``GET /v1/sessions`` so ``session_id``'s row carries the kept-label.

    :param page: Playwright page before navigation.
    :param session_id: Session whose list row should carry the label.
    """

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
    """A labeled archived session explains its kept worktree; a plain one doesn't.

    :param live_server: Spawned server fixture — base URL.
    :param page: Playwright page.
    """
    suffix = uuid.uuid4().hex[:8]
    kept_title = f"kept-worktree-{suffix}"
    plain_title = f"plain-archived-{suffix}"
    kept_id = _seed_archived_session(live_server, title=kept_title)
    plain_id = _seed_archived_session(live_server, title=plain_title)
    _inject_kept_label(page, kept_id)
    try:
        page.goto(f"{live_server}/settings/archived")

        kept_row = page.locator("li[data-testid='archived-row']", has_text=kept_title)
        note = kept_row.get_by_test_id("worktree-kept-note")
        expect(note).to_have_text(
            "Worktree kept — 2 uncommitted changes, 1 unpushed commit, "
            "branch not merged into origin/main."
        )

        plain_row = page.locator("li[data-testid='archived-row']", has_text=plain_title)
        expect(plain_row.get_by_test_id("worktree-kept-note")).to_have_count(0)
    finally:
        for session_id in (kept_id, plain_id):
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
