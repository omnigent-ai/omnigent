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


def test_a_step_time_limit_is_a_line_in_its_description(page: Page, live_server: str) -> None:
    """A step's time limit menu writes and clears the Time limit line in its description."""
    name = f"Fix flaky tests {uuid4().hex[:8]}"
    fix_goal = "**Outputs:** passing CI.\n\n**Details:** rerun the test."
    limited = "**Outputs:** passing CI.\n\n**Time limit:** 2 hours\n\n**Details:** rerun the test."
    steps = [
        {"id": "fix", "name": "Fix the test", "goal": fix_goal, "depends_on": []},
        {"id": "report", "name": "Report", "goal": None, "depends_on": ["fix"]},
    ]
    with httpx.Client(base_url=live_server, timeout=10.0) as client:
        response = client.post(
            "/v1/workflows", json={"name": name, "description": "Fix one test.", "steps": steps}
        )
        response.raise_for_status()
        workflow_id = response.json()["id"]
        try:
            page.goto(f"{live_server}/workflows/{workflow_id}")
            graph = page.get_by_test_id("workflow-steps-graph")
            expect(graph.get_by_test_id("workflow-step-node")).to_have_count(2)

            def choose(button: str, item: str) -> dict[str, str | None]:
                with page.expect_response(
                    lambda response: (
                        response.url == f"{live_server}/v1/workflows/{workflow_id}"
                        and response.request.method == "PATCH"
                    )
                ) as saved:
                    graph.get_by_role("button", name=button, exact=True).click()
                    page.get_by_role("menuitemradio", name=item, exact=True).click()
                assert saved.value.ok, saved.value.text()
                return {step["id"]: step["goal"] for step in saved.value.json()["steps"]}

            goals = choose("Set a time limit for Fix the test", "2 hours")
            assert goals == {"fix": limited, "report": None}
            fix_limit = graph.get_by_role(
                "button", name="Time limit for Fix the test: 2 hours", exact=True
            )
            expect(fix_limit).to_have_text("2h")

            page.reload()
            expect(fix_limit).to_have_text("2h")
            goals = choose("Time limit for Fix the test: 2 hours", "No limit")
            assert goals == {"fix": fix_goal, "report": None}
            expect(
                graph.get_by_role("button", name="Set a time limit for Fix the test", exact=True)
            ).to_be_visible()
            saved_steps = client.get(f"/v1/workflows/{workflow_id}").json()["steps"]
            assert [step["goal"] for step in saved_steps] == [fix_goal, None]
        finally:
            client.delete(f"/v1/workflows/{workflow_id}")
