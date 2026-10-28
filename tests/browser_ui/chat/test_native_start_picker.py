"""Native picker display and creation payloads require only a browser."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Request, expect

from tests._helpers.picker_routes import OWN_AGENTS
from tests.browser_ui.chat.session_contract import ChatSessionContract


def _open_picker(
    page: Page,
    chat: ChatSessionContract,
    harness: str,
    *,
    sdk_kimi: bool = False,
    models: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    agent_id = f"ag_{harness}_browser"
    wrapper = f"{harness}-ui"
    agents = [
        {
            "id": agent_id,
            "name": wrapper,
            "display_name": wrapper,
            "harness": harness,
            "skills": [],
        }
    ]
    if sdk_kimi:
        agents.append(
            {
                "id": "ag_kimi_sdk",
                "name": "kimi",
                "display_name": "Kimi",
                "harness": "kimi",
                "skills": [],
            }
        )
    contract = chat.contract
    contract.json("/v1/agents", {"data": agents})
    contract.json(OWN_AGENTS, {"data": []})
    contract.json("/v1/sessions", {"data": []})
    contract.json("/v1/skills", {"skills": []})
    contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": chat.host_id,
                    "name": "browser-host",
                    "owner": "browser",
                    "status": "online",
                    "configured_harnesses": {harness: True},
                }
            ]
        },
    )
    contract.json(
        re.compile(r"/v1/hosts/[^/]+/harnesses/[^/]+/model-options(?:\?.*)?$"), {"models": []}
    )
    contract.json(
        f"/v1/hosts/{chat.host_id}/harnesses/{harness}/model-options", {"models": models or []}
    )
    contract.json(
        re.compile(r"/v1/sandbox-providers/[^/]+/harnesses/[^/]+/model-options(?:\?.*)?$"),
        {
            "configured": False,
            "status": "unconfigured",
            "models": [],
            "configuration_revision": None,
            "provider_label": None,
            "default_model": None,
        },
    )
    contract.json(f"/v1/hosts/{chat.host_id}/worktrees", {"data": []})
    creates: list[dict[str, Any]] = []

    def create(request: Request) -> dict[str, str]:
        creates.append(request.post_data_json)
        return {"id": chat.session_id}

    contract.json("/v1/sessions", create, method="POST")
    recent = json.dumps({chat.host_id: ["/work/repo"]})
    page.add_init_script(
        f"localStorage.setItem('omnigent:recent-workspaces', JSON.stringify({recent}))"
    )
    page.goto(chat.base_url)
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible()
    return creates


@pytest.mark.parametrize(
    ("harness", "label"),
    [
        ("pi-native", "Pi"),
        ("antigravity-native", "Antigravity"),
        ("opencode-native", "OpenCode"),
        ("kimi-native", "Kimi"),
    ],
)
def test_native_picker_label_and_wrapper_payload(
    page: Page, chat_session_contract: ChatSessionContract, harness: str, label: str
) -> None:
    chat = chat_session_contract
    creates = _open_picker(page, chat, harness)
    chip = page.get_by_test_id("new-chat-landing-agent-select")
    expect(chip).to_have_attribute("aria-label", re.compile(label))
    expect(chip).not_to_have_attribute("aria-label", re.compile("native"))
    page.get_by_test_id("new-chat-landing-input").fill("explore the repo")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/sessions")
        )
    ):
        page.get_by_test_id("new-chat-landing-submit").click()
    assert len(creates) == 1
    body = creates[0]
    assert body["agent_id"] == f"ag_{harness}_browser", body
    assert body["host_id"] == chat.host_id, body
    assert body["workspace"] == "/work/repo", body
    assert body.get("labels") == {
        "omnigent.ui": "terminal",
        "omnigent.wrapper": f"{harness}-ui",
        "omnigent.client_create_token": body["labels"]["omnigent.client_create_token"],
        "omnigent.composer_context.v1.0": (
            '{"version":1,"working_directory":{"path":"/work/repo"},"worktree":{"mode":"none"}}'
        ),
    }, body
    assert re.fullmatch(r"[0-9a-f]{32}", body["labels"]["omnigent.client_create_token"])


def test_native_picker_hides_sdk_kimi(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    _open_picker(page, chat_session_contract, "kimi-native", sdk_kimi=True)
    page.get_by_test_id("new-chat-landing-agent-select").click()
    expect(page.get_by_test_id("new-chat-landing-agent-ag_kimi-native_browser")).to_be_visible()
    expect(page.get_by_test_id("new-chat-landing-harness-more")).to_have_count(0)
    expect(page.get_by_test_id("new-chat-landing-agent-ag_kimi_sdk")).to_have_count(0)
    expect(page.locator("[data-harness-menu-row]")).to_have_count(1)


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_codex_remembers_explicit_models_across_catalog_changes(
    page: Page, chat_session_contract: ChatSessionContract, width: int, tmp_path: Path
) -> None:
    """A named default is pinned; missing catalogs cannot silently replace it."""
    chat = chat_session_contract
    page.set_viewport_size({"width": width, "height": 900})
    preferred = {"id": "gpt-6.1-sol", "displayName": "GPT 6.1 Sol", "isDefault": True}
    fallback = {"id": "gpt-5.6-sol", "displayName": "GPT 5.6 Sol"}
    creates = _open_picker(page, chat, "codex-native", models=[preferred, fallback])
    picker = page.get_by_test_id("new-chat-landing-agent-select")

    def open_models() -> None:
        picker.click()
        page.get_by_test_id("new-chat-landing-agent-config-ag_codex-native_browser").click()

    def close_models() -> None:
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")

    def remembered_model() -> str:
        return page.evaluate(
            "JSON.parse(localStorage.getItem('omnigent:last-mode-by-harness'))['codex-native'].model"
        )

    catalog_path = f"/v1/hosts/{chat.host_id}/harnesses/codex-native/model-options"
    open_models()
    page.get_by_role("menuitemcheckbox", name="GPT 6.1 Sol", exact=True).click()
    close_models()
    assert remembered_model() == preferred["id"]

    # The provider changes its default and the spelling of the selected model.
    renamed = {**preferred, "id": "system.ai.gpt-6-1-sol", "isDefault": False}
    chat.contract.json(catalog_path, {"models": [{**fallback, "isDefault": True}, renamed]})
    page.reload()
    expect(picker).to_contain_text("GPT 6.1 Sol")
    open_models()
    expect(page.get_by_role("menuitemcheckbox", name="GPT 6.1 Sol", exact=True)).to_have_attribute(
        "aria-checked", "true"
    )
    expect(page.get_by_role("menuitemcheckbox", name="Default", exact=True)).to_have_attribute(
        "aria-checked", "false"
    )
    page.screenshot(path=tmp_path / "codex-remembered-model.png", animations="disabled")
    close_models()

    chat.contract.json(catalog_path, {"models": [{**fallback, "isDefault": True}]})
    page.reload()
    page.get_by_test_id("new-chat-landing-input").fill("Use my selected model")
    expect(page.get_by_role("alert")).to_contain_text("Choose another model or Default")
    expect(page.get_by_test_id("new-chat-landing-submit")).to_be_disabled()
    assert remembered_model() == preferred["id"]
    assert creates == []
    page.screenshot(path=tmp_path / "codex-missing-model.png", animations="disabled")

    chat.contract.json(catalog_path, {"models": [{**fallback, "isDefault": True}, renamed]})
    page.reload()
    expect(picker).to_contain_text("GPT 6.1 Sol")
    page.get_by_test_id("new-chat-landing-input").fill("Use my selected model")
    page.get_by_test_id("new-chat-landing-submit").click()
    expect(page).to_have_url(chat.url)
    assert len(creates) == 1
    assert creates[0]["model_override"] == renamed["id"]

    chat.contract.json(catalog_path, {"models": [{**fallback, "isDefault": True}]})
    page.goto(chat.base_url)
    expect(page.get_by_role("alert")).to_contain_text("Choose another model or Default")
    open_models()
    page.get_by_role("menuitemcheckbox", name="Default", exact=True).click()
    close_models()
    page.get_by_test_id("new-chat-landing-input").fill("Use the default this time")
    expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
    assert remembered_model() == ""
