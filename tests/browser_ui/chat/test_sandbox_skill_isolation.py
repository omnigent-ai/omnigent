"""Sandbox skill isolation and first prompts use mocked browser contracts."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from tests._helpers.picker_routes import OWN_AGENTS
from tests.browser_ui.chat.session_contract import ChatSessionContract


@pytest.mark.parametrize("harness,prefix", [("claude-native", "/"), ("codex-native", "$")])
def test_sandbox_first_prompt_does_not_use_connected_host_skills(
    page: Page, chat_session_contract: ChatSessionContract, harness: str, prefix: str
) -> None:
    page.set_viewport_size({"width": 1440, "height": 960})
    chat = chat_session_contract
    contract = chat.contract
    contract.json(
        "/v1/info",
        {
            "accounts_enabled": False,
            "single_user": True,
            "needs_setup": False,
            "managed_sandboxes_enabled": True,
            "sandbox_provider": "example",
            "durable_initial_input": True,
        },
    )
    contract.json(
        "/v1/agents",
        {
            "data": [
                {
                    "id": chat.agent_id,
                    "name": f"{harness}-ui",
                    "harness": harness,
                    "skills": [],
                }
            ]
        },
    )
    contract.json(OWN_AGENTS, {"data": []})
    contract.json("/v1/sessions", {"data": []})
    contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": chat.host_id,
                    "name": "connected workstation",
                    "owner": "local",
                    "status": "online",
                    "configured_harnesses": {harness: True},
                }
            ]
        },
    )
    discoveries: list[dict[str, list[str]]] = []
    creates: list[dict[str, Any]] = []

    def discover(request: Request) -> dict[str, Any]:
        discoveries.append(parse_qs(urlparse(request.url).query))
        return {
            "skills": [
                {"name": "dev:review", "description": "Review a change with your installed skill"}
            ]
        }

    def create(request: Request) -> dict[str, str]:
        creates.append(request.post_data_json)
        return {"id": chat.session_id}

    contract.json("/v1/skills", discover)
    contract.json("/v1/sessions", create, method="POST")
    page.goto(chat.base_url)
    composer = page.get_by_test_id("new-chat-landing-input")
    composer.fill(f"{prefix}review")
    expect(
        page.get_by_text("Host skills will be available after the sandbox starts.")
    ).to_be_visible()
    assert discoveries == []
    assert creates == []
    prompt = f"{prefix}dev:review check these changes"
    composer.fill(prompt)
    page.get_by_test_id("new-chat-landing-submit").click()
    expect(page).to_have_url(chat.url)
    assert len(creates) == 1
    assert creates[0]["host_type"] == "managed"
    assert creates[0]["sandbox_provider"] == "example"
    assert "host_id" not in creates[0]
    assert creates[0]["initial_input"] == {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
    }
