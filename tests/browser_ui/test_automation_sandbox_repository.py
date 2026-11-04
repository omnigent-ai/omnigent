"""Automation repository selection in the built SPA with synthetic Arclet contracts."""

from contextlib import contextmanager
from typing import Any

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.chat.session_contract import chat_session_handle
from tests.browser_ui.conftest import BrowserContract


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_automation_sandbox_repository(
    page: Page, browser_contract: BrowserContract, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    with contextmanager(chat_session_handle)(page, browser_contract):
        contract = browser_contract
        catalog = [
            {"name": name, "url": f"https://github.com/example/{name}"}
            for name in ("universe", "runtime", "ds-projects", "hadron", "neon-cloud")
        ]
        contract.json(
            "/v1/info",
            {
                "single_user": True,
                "automations_enabled": True,
                "managed_sandboxes_enabled": True,
                "scheduled_task_sandbox_workspaces": True,
                "sandbox_provider": "arclet",
                "sandbox_provider_capabilities": {"arclet": {"repo_catalog": catalog}},
                "features": {"automations_v2": True},
            },
        )
        tasks: list[dict[str, Any]] = []
        contract.json("/v1/scheduled-tasks", lambda _: {"scheduled_tasks": tasks})

        def create(request: Request) -> dict[str, Any]:
            body = request.post_data_json
            assert body["execution_target"] == "managed_sandbox"
            assert body["workspace"] == catalog[1]["url"]
            assert "host_id" not in body
            task = {
                "id": "catalog-automation",
                "owner_user_id": "local",
                "created_at": 1,
                "updated_at": 1,
                "model_override": None,
                "reasoning_effort": None,
                "permission_mode": None,
                "session_mode": "new",
                "session_id": None,
                "host_id": None,
                "project_id": None,
                "state": "active",
                "last_run_at": None,
                "last_run_status": None,
                "last_run_conversation_id": None,
                "next_run_at": None,
                **body,
            }
            tasks.append(task)
            return task

        def update(request: Request) -> dict[str, Any]:
            body = request.post_data_json
            assert body["workspace"] == (
                catalog[3]["url"] if tasks[0]["workspace"] == catalog[1]["url"] else None
            )
            assert "host_id" not in body
            tasks[0].update(body)
            return tasks[0]

        contract.json("/v1/scheduled-tasks", create, method="POST")
        contract.json("/v1/scheduled-tasks/catalog-automation", update, method="PATCH")
        page.goto(f"{contract.base_url}/tasks")
        page.get_by_role("button", name="Maybe later", exact=True).click()
        page.get_by_role("button", name="New automation", exact=True).click()
        page.get_by_label("Name", exact=True).fill("Repository maintenance")
        page.get_by_label("Prompt", exact=True).fill(
            "Review the repository for stale documentation."
        )
        page.get_by_label("Host", exact=True).click()
        page.get_by_role("option", name="Arclet Sandbox", exact=True).click()
        repository = page.get_by_label("Repository", exact=True)
        expect(repository).to_have_text("Provider default")
        repository.click()
        for entry in catalog:
            expect(page.get_by_role("option", name=entry["name"], exact=True)).to_be_visible()
        page.get_by_role("option", name="runtime", exact=True).click()
        expect(repository).to_have_text("runtime")
        expect(page.get_by_test_id("task-workspace-browse")).to_have_count(0)
        page.get_by_test_id("create-scheduled-task-submit").click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        page.get_by_role("tab", name="Your automations", exact=True).click()
        # Standalone server identity persists the initial intro dismissal.
        expect(page.get_by_role("dialog")).to_have_count(0)
        page.get_by_role("button", name="Actions for Repository maintenance").click()
        page.get_by_role("menuitem", name="Edit", exact=True).click()
        expect(repository).to_have_text("runtime")
        repository.click()
        page.get_by_role("option", name="hadron", exact=True).click()
        page.get_by_test_id("create-scheduled-task-submit").click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        assert tasks[0]["workspace"] == catalog[3]["url"]
        page.get_by_role("button", name="Actions for Repository maintenance").click()
        page.get_by_role("menuitem", name="Edit", exact=True).click()
        expect(repository).to_have_text("hadron")
        repository.click()
        page.get_by_role("option", name="Provider default", exact=True).click()
        page.get_by_test_id("create-scheduled-task-submit").click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        assert tasks[0]["workspace"] is None
        page.get_by_role("button", name="Actions for Repository maintenance").click()
        page.get_by_role("menuitem", name="Edit", exact=True).click()
        expect(repository).to_have_text("Provider default")
