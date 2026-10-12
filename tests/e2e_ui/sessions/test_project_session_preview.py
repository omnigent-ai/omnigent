"""An expanded project previews its recently updated sessions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle

_SESSION_COUNT = 6


@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "mobile"])
def test_project_session_preview(
    page: Page, live_server: str, mobile: bool, output_path: str
) -> None:
    project_name = f"Project preview {uuid.uuid4().hex[:6]}"
    session_ids: list[str] = []
    with httpx.Client(base_url=live_server, timeout=30.0) as client:
        created = client.post("/v1/projects", json={"name": project_name})
        created.raise_for_status()
        project_id = created.json()["id"]
        try:
            for index in range(_SESSION_COUNT):
                response = post_session_bundle(
                    client.post, "/v1/sessions", _build_hello_world_bundle()
                )
                response.raise_for_status()
                session_id = response.json()["session_id"]
                session_ids.append(session_id)
                client.patch(
                    f"/v1/sessions/{session_id}",
                    json={"title": f"Project session {index + 1}", "project_id": project_id},
                ).raise_for_status()

            page.set_viewport_size(
                {"width": 390, "height": 844} if mobile else {"width": 1280, "height": 800}
            )
            evidence = Path(output_path)
            evidence.mkdir(parents=True, exist_ok=True)

            def open_project() -> Locator:
                page.goto(f"{live_server}/?sidebar=open")
                header = page.get_by_role("button", name=project_name, exact=True)
                expect(header).to_be_visible()
                # Expansion persists across reloads; open the folder only once.
                if header.get_attribute("aria-expanded") != "true":
                    header.click()
                expect(header).to_have_attribute("aria-expanded", "true")
                project = header.locator("xpath=ancestor::section[1]")
                # Until the folder's own page arrives it shows only sidebar rows.
                expect(project.get_by_text("Loading…", exact=True)).to_have_count(0)
                return project

            # Every session was just updated, so the preview shows them all.
            project = open_project()
            rows = project.locator("[data-sidebar-session-id]")
            expect(rows).to_have_count(_SESSION_COUNT)
            expect(project.get_by_role("button", name="Show more", exact=True)).to_have_count(0)
            page.screenshot(path=str(evidence / "project-preview-recent.png"))

            # Four days later none is recent: the preview keeps the newest three,
            # in the server's order (sessions created in one second can tie).
            listed = client.get(
                "/v1/sessions",
                params={
                    "project": project_name,
                    "order": "desc",
                    "sort_by": "updated_at",
                    "visibility": "mine",
                },
            )
            listed.raise_for_status()
            newest = [session["id"] for session in listed.json()["data"]][:3]
            page.clock.install(time=datetime.now(UTC) + timedelta(days=4))
            project = open_project()
            rows = project.locator("[data-sidebar-session-id]")
            expect(rows).to_have_count(3)
            for index, session_id in enumerate(newest):
                expect(rows.nth(index)).to_have_attribute("data-sidebar-session-id", session_id)
            page.screenshot(path=str(evidence / "project-preview-stale.png"))
            project.get_by_role("button", name="Show more", exact=True).click()
            expect(rows).to_have_count(_SESSION_COUNT)
            page.screenshot(path=str(evidence / "project-preview-expanded.png"))
            project.get_by_role("button", name="Show less", exact=True).click()
            expect(rows).to_have_count(3)

            # Hiding rows never changes their project membership.
            for session_id in session_ids:
                persisted = client.get(f"/v1/sessions/{session_id}")
                persisted.raise_for_status()
                assert persisted.json()["project_id"] == project_id
        finally:
            # Attempt every deletion before reporting any that failed.
            responses = [client.delete(f"/v1/sessions/{sid}") for sid in session_ids]
            responses.append(client.delete(f"/v1/projects/{project_id}"))
            failed = [f"{r.request.url.path}: {r.status_code}" for r in responses if r.is_error]
            assert not failed, f"cleanup failed: {failed}"
