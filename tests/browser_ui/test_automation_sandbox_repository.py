"""Automation repository selection in the built SPA with synthetic Arclet contracts."""

from contextlib import contextmanager
from typing import Any

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.chat.session_contract import chat_session_handle, list_payload
from tests.browser_ui.conftest import BrowserContract


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_automation_sandbox_repository(
    page: Page, browser_contract: BrowserContract, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    with contextmanager(chat_session_handle)(page, browser_contract) as chat:
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
                "scheduled_task_session_modes": True,
                "scheduled_task_projects": True,
                "scheduled_task_subhour": True,
                "sandbox_provider": "arclet",
                "sandbox_provider_capabilities": {"arclet": {"repo_catalog": catalog}},
                "picker": {
                    "restrict_to_agent_names": [
                        "claude-native-ui",
                        "codex-native-ui",
                        "pi-native-ui",
                    ],
                    "default_agent_name": "claude-native-ui",
                },
                "features": {"automations_v2": True},
            },
        )
        contract.json(
            "/v1/agents",
            list_payload(
                [
                    {
                        "id": f"agent-{harness}",
                        "name": f"{harness}-native-ui",
                        "harness": f"{harness}-native",
                        "builtin": True,
                    }
                    for harness in ("claude", "codex", "pi", "cursor", "opencode")
                ]
            ),
        )
        contract.json(
            "/v1/projects",
            list_payload(
                [{"id": "automation-project", "name": "Omnigent Automation", "icon": None}]
            ),
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
        expect(page.get_by_label("Instructions", exact=True)).to_be_focused()
        expect(page.get_by_label("Name", exact=True)).to_be_visible()
        expect(page.get_by_label("Run on", exact=True)).to_be_visible()
        expect(page.get_by_role("switch", name="Start each run in a new session")).to_be_visible()
        expect(page.get_by_role("button", name="Advanced", exact=True)).to_have_attribute(
            "aria-expanded", "false"
        )
        expect(page.get_by_label("Frequency", exact=True)).to_be_visible()
        expect(page.get_by_label("Time", exact=True)).to_be_visible()
        page.get_by_role("button", name="Advanced", exact=True).click()
        page.get_by_label("Name", exact=True).fill("Repository maintenance")
        page.get_by_label("Instructions", exact=True).fill(
            "Review the repository for stale documentation."
        )
        page.get_by_label("Run on", exact=True).click()
        page.get_by_role("option", name="Arclet Sandbox", exact=True).click()
        page.get_by_test_id("task-agent-picker").get_by_role("button").click()
        for harness in ("claude", "codex", "pi"):
            expect(page.get_by_test_id(f"new-chat-landing-agent-agent-{harness}")).to_be_visible()
        for harness in ("cursor", "opencode"):
            expect(page.get_by_test_id(f"new-chat-landing-agent-agent-{harness}")).to_have_count(0)
        page.get_by_test_id("new-chat-landing-agent-agent-claude").click()
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
        page.get_by_role("button", name="Advanced", exact=True).click()
        page.get_by_test_id("task-agent-picker").get_by_role("button").click()
        expect(page.get_by_test_id("new-chat-landing-agent-agent-pi")).to_be_visible()
        expect(page.get_by_test_id("new-chat-landing-agent-agent-cursor")).to_have_count(0)
        page.get_by_test_id("new-chat-landing-agent-agent-claude").click()
        expect(repository).to_have_text("runtime")
        repository.click()
        page.get_by_role("option", name="hadron", exact=True).click()
        page.get_by_test_id("create-scheduled-task-submit").click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        assert tasks[0]["workspace"] == catalog[3]["url"]
        page.get_by_role("button", name="Actions for Repository maintenance").click()
        page.get_by_role("menuitem", name="Edit", exact=True).click()
        page.get_by_role("button", name="Advanced", exact=True).click()
        expect(repository).to_have_text("hadron")
        repository.click()
        page.get_by_role("option", name="Provider default", exact=True).click()
        page.get_by_test_id("create-scheduled-task-submit").click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        assert tasks[0]["workspace"] is None
        page.get_by_role("button", name="Actions for Repository maintenance").click()
        page.get_by_role("menuitem", name="Edit", exact=True).click()
        page.get_by_role("button", name="Advanced", exact=True).click()
        expect(repository).to_have_text("Provider default")
        # Portal dismissal needs a browser, without a live scheduler or host process.
        page.get_by_label("Run on", exact=True).click()
        page.get_by_role("option", name="Browser host", exact=True).click()
        for trigger_id, role in (
            ("task-host-trigger", "listbox"),
            ("task-model-trigger", "listbox"),
            ("task-effort-trigger", "listbox"),
            ("task-permission-trigger", "listbox"),
            ("task-project-trigger", "menu"),
            ("new-chat-landing-agent-select", "menu"),
        ):
            page.get_by_test_id(trigger_id).click()
            expect(page.get_by_role(role)).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.get_by_role(role)).to_have_count(0)
            expect(page.get_by_test_id("create-scheduled-task-dialog")).to_be_visible()
            expect(page.get_by_label("Name", exact=True)).to_have_value("Repository maintenance")
        contract.json(f"/v1/hosts/{chat.host_id}/filesystem", list_payload([]))
        for _ in range(2):
            page.get_by_test_id("task-workspace-browse").click()
            expect(page.get_by_test_id("workspace-picker-dialog")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.get_by_test_id("workspace-picker-dialog")).to_have_count(0)
            expect(page.get_by_label("Name", exact=True)).to_have_value("Repository maintenance")
        page.get_by_role("button", name="Cancel", exact=True).click()


@pytest.mark.parametrize("prompt_kind", ["scheduled", "trigger", "custom-trigger"])
def test_reused_automation_settings(
    page: Page, browser_contract: BrowserContract, prompt_kind: str
) -> None:
    trigger = prompt_kind != "scheduled"
    with contextmanager(chat_session_handle)(page, browser_contract) as chat:
        contract = browser_contract
        chat.harness = "claude-native"
        contract.json(
            "/v1/info",
            {
                "single_user": True,
                "automations_enabled": True,
                "scheduled_task_session_modes": True,
                "features": {"automations_v2": True},
            },
        )
        prompt = (
            "This automation watches Jira and starts one Omnigent session "
            "for each new ticket.\n\n"
            "Each run:\n1. For each other ticket, oldest first, call sys_session_create with "
            'agent_id set to your agent ID, placement "same_host", title "[Jira] <key>", '
            "and a message under 8000 characters.\n\n"
            "Instructions for each session:\nTriage the ticket.\n\n"
            "Posting instructions for each session:\nSign the reply."
            if trigger
            else "Summarize changes."
        )
        if prompt_kind == "custom-trigger":
            prompt += "\n\nInstructions for each session:\nExtra heading."
        task = {
            "id": "settings-automation",
            "name": "Repository maintenance",
            "owner_user_id": "local",
            "created_at": 1,
            "updated_at": 1,
            "agent_id": chat.agent_id,
            "prompt": prompt,
            "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "timezone": "UTC",
            "model_override": None,
            "reasoning_effort": None,
            "permission_mode": None,
            "session_mode": "continue",
            "session_id": chat.session_id,
            "host_id": None,
            "project_id": None,
            "execution_target": "connected_host",
            "workspace": None,
            "state": "active",
            "last_run_at": None,
            "last_run_status": None,
            "last_run_conversation_id": None,
            "next_run_at": None,
        }
        contract.json("/v1/scheduled-tasks", {"scheduled_tasks": [task]})
        if prompt_kind == "trigger":
            contract.json(
                "/v1/agents",
                list_payload(
                    [
                        {
                            "id": chat.agent_id,
                            "name": "codex-native-ui",
                            "harness": "codex-native",
                            "builtin": True,
                        },
                        {
                            "id": "worker-claude",
                            "name": "claude-native-ui",
                            "harness": "claude-native",
                            "builtin": True,
                        },
                    ]
                ),
            )
            contract.json("/v1/scheduled-tasks/settings-automation", lambda _: task)
            contract.json("/v1/scheduled-tasks/settings-automation/runs", {"runs": []})

            def update(request: Request) -> dict[str, Any]:
                body = request.post_data_json
                assert not {
                    "agent_id",
                    "model_override",
                    "reasoning_effort",
                    "permission_mode",
                    "session_id",
                }.intersection(body)
                assert 'agent_id "worker-claude"' in body["prompt"]
                assert 'model "opus", reasoning_effort "high"' in body["prompt"]
                assert "Triage the ticket." in body["prompt"]
                task.update(body)
                return task

            contract.json("/v1/scheduled-tasks/settings-automation", update, method="PATCH")
        page.goto(f"{contract.base_url}/tasks")
        page.get_by_role("button", name="Maybe later", exact=True).click()
        page.get_by_role("button", name="Actions for Repository maintenance").click()
        page.get_by_role("menuitem", name="Edit", exact=True).click()
        page.get_by_role("button", name="Advanced", exact=True).click()
        if prompt_kind == "trigger":
            worker_agent = page.get_by_test_id("task-worker-agent-trigger")
            expect(worker_agent).to_contain_text("Codex")
            worker_agent.click()
            page.keyboard.press("Escape")
            expect(page.get_by_role("listbox")).to_have_count(0)
            expect(page.get_by_test_id("create-scheduled-task-dialog")).to_be_visible()
            worker_agent.click()
            page.get_by_role("option", name="Claude Code", exact=True).click()
            page.get_by_test_id("task-model-trigger").click()
            page.get_by_role("option", name="Opus", exact=True).click()
            page.get_by_test_id("task-effort-trigger").click()
            page.get_by_role("option", name="High", exact=True).click()
            page.get_by_test_id("create-scheduled-task-submit").click()
            expect(page.get_by_role("dialog")).to_have_count(0)
            assert task["session_id"] == chat.session_id
            assert task["agent_id"] == chat.agent_id
            page.get_by_test_id("task-row-open").click()
            page.get_by_test_id("task-detail-edit").click()
            page.get_by_role("button", name="Advanced", exact=True).click()
            expect(worker_agent).to_contain_text("Claude Code")
            expect(page.get_by_test_id("task-model-trigger")).to_have_text("Opus")
            expect(page.get_by_test_id("task-effort-trigger")).to_have_text("High")
            page.get_by_test_id("task-worker-settings").scroll_into_view_if_needed()
            return
        note = page.get_by_test_id("task-session-settings-note")
        note.scroll_into_view_if_needed()
        expect(note).to_contain_text("This automation continues in an existing session")
        expect(page.get_by_test_id("task-model-trigger")).to_be_disabled()
        if trigger:
            expect(note).to_contain_text("each match starts a new worker")
            page.get_by_role("button", name="Edit worker instructions", exact=True).click()
            # Only a prompt that can't be split keeps the creation call in the Instructions field.
            editor = page.get_by_role("textbox", name="Instructions", exact=True)
            expect(editor).to_be_focused()
            page.get_by_test_id("task-name-input").click()
            page.get_by_role("button", name="Edit worker instructions", exact=True).click()
            expect(editor).to_be_focused()
        else:
            expect(
                page.get_by_role("button", name="Edit worker instructions", exact=True)
            ).to_have_count(0)


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_automation_pending_approval_links_to_session(
    page: Page, browser_contract: BrowserContract, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    with contextmanager(chat_session_handle)(page, browser_contract) as chat:
        contract = browser_contract
        contract.json(
            "/v1/info",
            {
                "single_user": True,
                "automations_enabled": True,
                "features": {"automations_v2": True},
            },
        )
        task = {
            "id": "approval-task",
            "name": "Repository maintenance",
            "prompt": "Review stale documentation.",
            "agent_id": chat.agent_id,
            "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "timezone": "UTC",
            "owner_user_id": "local",
            "created_at": 1,
            "updated_at": 1,
            "state": "active",
            "session_mode": "continue",
            "session_id": chat.session_id,
            "execution_target": "connected_host",
            "next_run_at": None,
        }
        run = {
            "id": "approval-run",
            "scheduled_task_id": task["id"],
            "status": "succeeded",
            "scheduled_at": 1,
            "fired_at": 1,
            "finished_at": 2,
            "conversation_id": chat.session_id,
            "conversation_pending_approvals": 1,
        }
        contract.json("/v1/scheduled-tasks/approval-task", task)
        contract.json(
            "/v1/scheduled-tasks/approval-task/runs", {"runs": [run], "next_cursor": None}
        )
        page.goto(f"{contract.base_url}/tasks/approval-task")
        notice = page.get_by_test_id("run-pending-approval")
        expect(notice).to_have_text("Session needs approval · Open to review")
        page.get_by_test_id("run-open").click()
        expect(page).to_have_url(chat.url)
        page.go_back()
        expect(notice).to_have_text("Session needs approval · Open to review")
        page.get_by_test_id("run-open").hover()
        expect(notice).to_be_visible()
