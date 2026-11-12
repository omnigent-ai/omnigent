"""E2E: a session change refetches only the sidebar lists it can affect.

Reconcile only lists whose membership or pagination may have changed. Three
project folders stay open while another client (the API) changes sessions; each
folder must show the right sessions, and only the folders a change can move a
session in or out of are fetched again.
"""

from __future__ import annotations

import json
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Locator, Page, Request, expect

from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle
from tests.e2e_ui.sessions._sidebar_helpers import enable_grouping_ui, patch_session

# The server rescans watched sessions every 4 s; this covers a frame, the
# reconcile debounce and the refetch it starts.
SETTLE_MS = 7_000


def _folder_link(page: Page, project: str, session_id: str) -> Locator:
    return _folder(page, project).locator(f'a[href="/c/{session_id}"]')


def _full_reconcile(fetched: list[str], names: list[str]) -> bool:
    """A full reconcile re-reads every expanded folder in one round."""
    return set(names) <= set(fetched)


def _folder(page: Page, project: str) -> Locator:
    """The project's own folder section, not the Projects group around it."""
    new_session = page.get_by_role("link", name=f"New session in {project}", exact=True)
    return new_session.locator("xpath=ancestor::section[1]")


def test_session_changes_refetch_only_the_folders_they_touch(
    page: Page, request: pytest.FixtureRequest
) -> None:
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    tag = uuid.uuid4().hex[:6]
    names = [f"Targeted {tag} {letter}" for letter in "ABC"]
    project_ids: dict[str, str] = {}
    members: dict[str, list[str]] = {}
    created: list[str] = []
    bundle = _build_hello_world_bundle()
    for name in names:
        response = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
        response.raise_for_status()
        project_ids[name] = response.json()["id"]
        members[name] = []
        for i in range(2):
            response = post_session_bundle(
                httpx.post, f"{base_url}/v1/sessions", bundle, timeout=30.0
            )
            response.raise_for_status()
            session_id = response.json()["session_id"]
            created.append(session_id)
            patch_session(
                base_url, session_id, project_id=project_ids[name], title=f"{name} row {i}"
            )
            members[name].append(session_id)
    a, b, c = names

    folder_fetches: list[str] = []

    def record(req: Request) -> None:
        parsed = urlparse(req.url)
        project = parse_qs(parsed.query).get("project")
        if req.method == "GET" and parsed.path == "/v1/sessions" and project:
            folder_fetches.append(project[0])

    page.add_init_script(
        "window.localStorage.setItem('omnigent:expanded-project-sections', "
        f"{json.dumps(json.dumps(names))});"
    )
    enable_grouping_ui(page)
    page.on("request", record)
    # Targeted rounds don't reset the 60 s full-reconcile clock, so one can land
    # in a window; that window's exact-set check is skipped.
    checked_windows = 0
    try:
        page.goto(base_url)
        for name in names:
            for session_id in members[name]:
                expect(_folder_link(page, name, session_id)).to_be_visible(timeout=20_000)
        page.wait_for_timeout(SETTLE_MS)

        # A rename and a label no list filters on are patched into the row in place.
        renamed = members[a][0]
        folder_fetches.clear()
        patch_session(base_url, renamed, labels={"e2e.status": "failing"})
        patch_session(base_url, renamed, title=f"{a} renamed")
        expect(_folder_link(page, a, renamed)).to_contain_text(f"{a} renamed", timeout=15_000)
        page.wait_for_timeout(SETTLE_MS)
        if not _full_reconcile(folder_fetches, names):
            assert folder_fetches == []
            checked_windows += 1

        # A move refetches the folder it left and the one it joined, not the third.
        moved = members[a][1]
        folder_fetches.clear()
        patch_session(base_url, moved, project_id=project_ids[b], labels={"omni_project": ""})
        expect(_folder_link(page, b, moved)).to_be_visible(timeout=15_000)
        expect(_folder_link(page, a, moved)).to_have_count(0)
        page.wait_for_timeout(SETTLE_MS)
        # Retries may repeat a fetch; both folders must be re-read, and only them.
        if not _full_reconcile(folder_fetches, names):
            assert set(folder_fetches) == {a, b}
            checked_windows += 1

        # An archive drops the row and refetches only its own folder.
        archived = members[c][0]
        folder_fetches.clear()
        patch_session(base_url, archived, archived=True)
        expect(_folder_link(page, c, archived)).to_have_count(0, timeout=15_000)
        page.wait_for_timeout(SETTLE_MS)
        if not _full_reconcile(folder_fetches, names):
            assert set(folder_fetches) == {c}  # backfills the emptied slot
            checked_windows += 1
        assert checked_windows > 0, "every watch window hit a full reconcile"

        # Every folder still lists exactly what the server holds for it.
        expect(_folder_link(page, a, renamed)).to_be_visible()
        expect(_folder_link(page, b, members[b][0])).to_be_visible()
        expect(_folder_link(page, b, members[b][1])).to_be_visible()
        expect(_folder_link(page, b, moved)).to_be_visible()
        expect(_folder_link(page, c, members[c][1])).to_be_visible()
    finally:
        for session_id in created:
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for project_id in project_ids.values():
            httpx.delete(f"{base_url}/v1/projects/{project_id}", timeout=10.0)
