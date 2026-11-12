"""Nested sandbox composers preserve the source directory across host switches."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from playwright.sync_api import Page, Request, expect

from tests._helpers.picker_routes import OWN_AGENTS
from tests.browser_ui.chat.session_contract import ChatSessionContract


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
@pytest.mark.parametrize("new_worktree", [False, True], ids=["reuse", "new-worktree"])
def test_nested_sandbox_keeps_source_directory(
    page: Page, chat_session_contract: ChatSessionContract, width: int, new_worktree: bool
) -> None:
    chat = chat_session_contract
    contract = chat.contract
    source_directory = "/work/repo-worktrees/research/src"
    other_directory = "/work/other-repo"
    page.set_viewport_size({"width": width, "height": 900})
    chat.update_session(title="Research", workspace=source_directory)
    contract.json(
        "/v1/info",
        {
            "single_user": True,
            "sibling_sessions": True,
            "managed_sandboxes_enabled": False,
        },
    )
    contract.json(OWN_AGENTS, {"data": []})
    contract.json("/v1/skills", {"skills": []})
    contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": host_id,
                    "name": name,
                    "owner": "local",
                    "status": "online",
                    "sandbox_provider": provider,
                    "configured_harnesses": {chat.harness: True},
                }
                for host_id, name, provider in (
                    (chat.host_id, "Research sandbox", "test-sandbox"),
                    ("other-host", "My computer", None),
                )
            ]
        },
    )
    contract.json(
        f"/v1/hosts/{chat.host_id}/worktrees",
        {
            "data": [
                {"path": "/work/repo", "branch": "master", "is_main": True},
                {
                    "path": "/work/repo-worktrees/research",
                    "branch": "feature/research",
                    "is_main": False,
                },
            ]
        },
    )
    contract.json(
        "/v1/hosts/other-host/worktrees",
        {"data": [{"path": other_directory, "branch": "main", "is_main": True}]},
    )
    contract.json(
        re.compile(r"/v1/hosts/other-host/harnesses/[^/]+/model-options(?:\?.*)?$"),
        {"models": chat.models},
    )
    recent = json.dumps({chat.host_id: ["/work/unrelated"], "other-host": [other_directory]})
    page.add_init_script(
        f"localStorage.setItem('omnigent:recent-workspaces', {json.dumps(recent)});"
        "localStorage.setItem('omnigent:always-use-worktree', 'true');"
        "localStorage.setItem('omnigent:default-base-branch', 'main');"
    )
    creates: list[dict[str, Any]] = []

    def create(request: Request) -> dict[str, str]:
        creates.append(request.post_data_json)
        return {"id": chat.session_id}

    contract.json("/v1/sessions", create, method="POST")
    page.goto(f"{chat.base_url}/?sibling={chat.session_id}")
    directory = page.get_by_test_id("new-chat-landing-workspace-chip")
    expect(directory).to_have_attribute("aria-label", f"Working directory: {source_directory}")
    worktree = page.get_by_test_id("new-chat-landing-branch-chip")
    expect(worktree).to_have_attribute("aria-label", "Existing worktree branch: feature/research")

    page.get_by_test_id("new-chat-landing-host-chip").click()
    page.get_by_test_id("new-chat-landing-host-other-host").click()
    expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_have_attribute(
        "aria-label", f"Working directory: {other_directory}"
    )
    expect(worktree).to_have_text("None")
    worktree.click()
    page.get_by_test_id("new-chat-landing-branch-input").fill("feature/manual-choice")
    page.keyboard.press("Escape")
    expect(page.get_by_test_id("new-chat-landing-branch-input")).to_have_count(0)
    page.get_by_test_id("new-chat-landing-host-chip").click()
    expect(page.get_by_test_id("new-chat-landing-host-menu")).to_be_visible()
    page.get_by_test_id(f"new-chat-landing-host-{chat.host_id}").click()
    expect(directory).to_have_attribute("aria-label", f"Working directory: {source_directory}")
    expect(worktree).to_have_attribute("aria-label", "Existing worktree branch: feature/research")
    worktree.click()
    expect(page.get_by_test_id("new-chat-landing-branch-input")).to_have_value("")
    expect(
        page.get_by_test_id("new-chat-landing-worktree-option").get_by_role("radio")
    ).to_be_checked()
    if new_worktree:
        page.get_by_test_id("new-chat-landing-branch-input").fill("feature/nested")
        expect(page.get_by_test_id("new-chat-landing-base-branch-input")).to_have_value(
            "feature/research"
        )
    page.keyboard.press("Escape")
    expect(page.get_by_test_id("new-chat-landing-branch-input")).to_have_count(0)
    page.get_by_test_id("new-chat-landing-input").fill("Review the changes in this checkout")
    page.get_by_test_id("new-chat-landing-submit").click()
    expect(page).to_have_url(chat.url)
    chat.wait_for_stream()
    expect(page.get_by_text("Review the changes in this checkout", exact=True)).to_be_visible()
    assert len(creates) == 1
    assert creates[0]["sibling_source_id"] == chat.session_id
    assert creates[0]["host_id"] == chat.host_id
    assert creates[0]["workspace"] == source_directory
    assert creates[0]["git"] == (
        {"branch_name": "feature/nested", "base_branch": "feature/research"}
        if new_worktree
        else {"branch_name": "feature/research", "existing_worktree": True}
    )
