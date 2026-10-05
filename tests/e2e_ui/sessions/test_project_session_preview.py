"""Project previews keep long sidebars short on desktop and mobile."""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle


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
            for index in range(8):
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
            page.goto(f"{live_server}/?sidebar=open")
            header = page.get_by_role("button", name=project_name, exact=True)
            expect(header).to_be_visible()
            header.click()
            project = header.locator("xpath=ancestor::section[1]")
            rows = project.locator("[data-sidebar-session-id]")
            expect(rows).to_have_count(5)
            evidence = Path(output_path)
            evidence.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(evidence / "project-preview.png"))
            project.get_by_role("button", name="Show more", exact=True).click()
            expect(rows).to_have_count(8)
            page.screenshot(path=str(evidence / "project-expanded.png"))
            project.get_by_role("button", name="Show less", exact=True).click()
            expect(rows).to_have_count(5)

            for session_id in session_ids:
                persisted = client.get(f"/v1/sessions/{session_id}")
                persisted.raise_for_status()
                assert persisted.json()["project_id"] == project_id
        finally:
            for session_id in session_ids:
                client.delete(f"/v1/sessions/{session_id}").raise_for_status()
            client.delete(f"/v1/projects/{project_id}").raise_for_status()
