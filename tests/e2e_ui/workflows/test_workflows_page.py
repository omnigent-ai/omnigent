"""Workflow menu actions through the built SPA and real server."""

from uuid import uuid4

import httpx
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.min_server_version("0.23.0")


@pytest.mark.parametrize("draft", [False, True], ids=["saved-plan", "draft"])
def test_clone_workflow(page: Page, seeded_session: tuple[str, str], draft: bool) -> None:
    """Clone persists an independent plan and opens it without the source's chat."""
    base_url, planning_session_id = seeded_session
    name = f"Review changes {uuid4().hex[:8]}"
    description = "Review open changes and write a summary."
    steps = (
        []
        if draft
        else [
            {"id": "review", "name": "Review changes", "goal": "Find bugs", "depends_on": []},
            {
                "id": "summarize",
                "name": "Write summary",
                "goal": "Summarize findings",
                "depends_on": ["review"],
            },
        ]
    )
    workflow_ids: list[str] = []
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        try:
            response = client.post(
                "/v1/workflows", json={"name": name, "description": description, "steps": steps}
            )
            response.raise_for_status()
            source_id = response.json()["id"]
            workflow_ids.append(source_id)
            response = client.patch(
                f"/v1/workflows/{source_id}", json={"planning_session_id": planning_session_id}
            )
            response.raise_for_status()
            source = response.json()

            page.goto(f"{base_url}/workflows")
            page.get_by_role("button", name=f"Actions for {name}", exact=True).click()
            with page.expect_response(
                lambda response: (
                    response.url == f"{base_url}/v1/workflows"
                    and response.request.method == "POST"
                )
            ) as created:
                page.get_by_role("menuitem", name="Clone", exact=True).click()
            assert created.value.ok, created.value.text()
            copy = created.value.json()
            workflow_ids.append(copy["id"])
            assert copy["id"] != source_id
            assert copy["name"] == f"{name} (copy)"
            assert copy["description"] == source["description"]
            assert copy["steps"] == source["steps"]
            assert copy["is_draft"] is draft
            assert copy["planning_session_id"] is None

            expect(page).to_have_url(f"{base_url}/workflows/{copy['id']}")
            page.reload()
            expect(page.get_by_test_id("workflow-detail-title")).to_have_text(copy["name"])
            expect(page.get_by_text(description, exact=True)).to_be_visible()
            if draft:
                expect(page.get_by_text("No plan yet", exact=True)).to_be_visible()
                expect(page.get_by_test_id("workflow-detail-run")).to_be_disabled()
            else:
                expect(page.get_by_test_id("workflow-step-node")).to_have_count(2)
                expect(page.get_by_test_id("workflow-detail-run")).to_be_enabled()
                expect(page.get_by_test_id("workflow-runs-empty")).to_be_visible()

            page.get_by_test_id("workflow-detail-back").click()
            expect(page.get_by_role("button", name=f"Open {name}", exact=True)).to_be_visible()
            expect(
                page.get_by_role("button", name=f"Open {copy['name']}", exact=True)
            ).to_be_visible()
            assert client.get(f"/v1/workflows/{source_id}").json() == source
            assert client.get(f"/v1/workflows/{copy['id']}").json() == copy
        finally:
            for workflow_id in workflow_ids:
                client.delete(f"/v1/workflows/{workflow_id}")
