"""Sandbox repository selection through the built SPA and mocked API contracts."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    install_chat_session_routes,
    list_payload,
    message_item,
)

_REPOSITORIES = (
    "https://github.com/acme/delivery-api.git#main",
    "https://github.com/acme/customer-web.git#main",
)
_FORK_ID = "browser-sandbox-fork"


def _sandbox_source(chat: ChatSessionContract) -> list[dict[str, Any]]:
    """Seed a multi-repo source and capture its fork request without provisioning."""
    chat.update_session(
        title="Delivery checkout improvements",
        host_id=None,
        workspace="/workspace",
        labels={"omnigent.sandbox.repo": " ".join(_REPOSITORIES)},
    )
    chat.set_items(
        [
            message_item(
                "checkout-answer",
                "assistant",
                "The API and customer web changes are ready for review. "
                "Fork this conversation to try the changes with another repository.",
                response_id="checkout-response",
            ),
            message_item(
                "checkout-prompt",
                "user",
                "Review the checkout updates across delivery-api and customer-web.",
                response_id="checkout-response",
            ),
        ]
    )
    fork = ChatSessionContract(
        page=chat.page,
        contract=chat.contract,
        stream=chat.stream,
        session_id=_FORK_ID,
    )
    fork.update_session(title="Fork of Delivery checkout improvements", host_id=None)
    fork.set_items(chat._items)
    install_chat_session_routes(fork)
    contract = chat.contract
    contract.json(
        "/v1/info",
        {
            "accounts_enabled": False,
            "single_user": True,
            "needs_setup": False,
            "managed_sandboxes_enabled": True,
            "session_fork_workspaces": True,
            "sandbox_provider": "agent_sandbox",
            "sandbox_providers": ["agent_sandbox", "modal"],
            "sandbox_provider_capabilities": {
                "agent_sandbox": {"multi_repo": True},
                "modal": {"multi_repo": False},
            },
            "features": {},
            "enabled_connections": ["github"],
            "harness_install_enabled": False,
            "installable_harnesses": [],
        },
    )
    contract.json("/v1/hosts", {"hosts": []})
    contract.json("/v1/sessions", lambda _: list_payload([chat.session_snapshot()]))
    contract.json(
        "/health",
        {"sessions": {chat.session_id: {"runner_online": True, "host_online": True}}},
    )
    contract.json(
        f"/v1/sessions/{chat.session_id}/resources/repository-branches",
        {
            "repositories": [
                {
                    "url": "https://github.com/acme/delivery-api.git",
                    "branch": "feature/checkout",
                    "pushed": True,
                },
                {
                    "url": "https://github.com/acme/customer-web.git",
                    "branch": "wip/customer-checkout",
                    "pushed": False,
                },
            ]
        },
    )
    contract.json(
        "/v1/connections/github/repos",
        {
            "connected": True,
            "repos": [
                {
                    "full_name": f"acme/{name}",
                    "clone_url": f"https://github.com/acme/{name}.git",
                    "default_branch": "main",
                    "private": True,
                    "pushed_at": None,
                }
                for name in ("delivery-api", "customer-web", "shared-contracts")
            ],
        },
    )
    for name in ("delivery-api", "customer-web", "shared-contracts"):
        contract.json(
            f"/v1/connections/github/repos/acme/{name}/branches",
            {"connected": True, "branches": ["main", "feature/checkout", "release/stable"]},
        )
    requests: list[dict[str, Any]] = []

    def create_fork(request: Request) -> dict[str, Any]:
        body = request.post_data_json
        requests.append(body)
        fork.update_session(
            labels={"omnigent.sandbox.repo": " ".join(body.get("workspaces") or [])}
        )
        return {"id": _FORK_ID}

    contract.json(f"/v1/sessions/{chat.session_id}/fork", create_fork, method="POST")
    return requests


def _open_fork(page: Page, session_id: str, entry: str) -> None:
    if entry == "message":
        page.locator('[data-testid="message-bubble"][data-role="assistant"]').first.hover()
        page.get_by_test_id("fork-from-response").first.click()
    elif entry.startswith("sidebar"):
        row = page.locator(f'li[data-sidebar-session-id="{session_id}"]')
        row.hover()
        if entry == "sidebar-context":
            row.click(button="right")
        else:
            row.get_by_test_id("conversation-actions").click()
        page.get_by_role("menuitem", name="Fork", exact=True).click()
    else:
        page.get_by_test_id("header-conversation-actions").click()
        page.get_by_role("menuitem", name="Fork", exact=True).click()
    expect(page.get_by_test_id("fork-session-dialog")).to_be_visible()


@pytest.mark.parametrize(
    "entry", ["header", "message", "sidebar-menu", "sidebar-context", "mobile-header"]
)
def test_sandbox_fork_edits_multiple_repositories_and_branches(
    page: Page,
    chat_session_contract: ChatSessionContract,
    entry: str,
) -> None:
    """Fork entries share prefill; desktop/mobile edit and message forks retain cutoff."""
    chat = chat_session_contract
    page.set_viewport_size(
        {"width": 390, "height": 844}
        if entry == "mobile-header"
        else {"width": 1280, "height": 900}
    )
    requests = _sandbox_source(chat)
    page.goto(chat.url)
    _open_fork(page, chat.session_id, entry)
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog.get_by_test_id("fork-session-sandbox-repo-row")).to_have_count(2)
    expect(dialog.get_by_role("combobox", name="Branch for acme/delivery-api")).to_have_text(
        "feature/checkout"
    )
    expect(dialog.get_by_role("combobox", name="Branch for acme/customer-web")).to_have_text(
        "main"
    )
    branch_note = dialog.get_by_text("“wip/customer-checkout” isn’t pushed. Using main.")
    expect(branch_note).to_be_visible()

    if entry.startswith("sidebar"):
        assert requests == []
        assert chat.session_patches == []
        return

    expected_workspaces = [
        "https://github.com/acme/delivery-api.git#feature/checkout",
        "https://github.com/acme/customer-web.git#main",
    ]
    if entry != "message":
        dialog.get_by_role("combobox", name="Branch for acme/delivery-api").click()
        page.get_by_role("option", name="release/stable", exact=True).click()
        expect(branch_note).to_be_visible()
        dialog.get_by_role("button", name="Remove acme/customer-web").click()
        expect(branch_note).to_have_count(0)
        dialog.get_by_role("combobox", name="GitHub repository").click()
        page.get_by_placeholder("Search repositories…").fill("shared")
        page.get_by_role("option", name="acme/shared-contracts").click()
        dialog.get_by_role("combobox", name="Branch for acme/shared-contracts").click()
        page.get_by_placeholder("Search or enter a branch…").fill("release/checkout")
        page.get_by_role("option", name="Use “release/checkout”").click()
        expect(dialog.get_by_test_id("fork-session-sandbox-repo-row")).to_have_count(2)
        expected_workspaces = [
            "https://github.com/acme/delivery-api.git#release/stable",
            "https://github.com/acme/shared-contracts.git#release/checkout",
        ]

    dialog.get_by_test_id("fork-session-submit").click()
    expect(page).to_have_url(f"{chat.base_url}/c/{_FORK_ID}")
    assert len(requests) == 1
    assert requests[0]["workspaces"] == expected_workspaces
    assert requests[0]["sandbox_provider"] == "agent_sandbox"
    assert requests[0].get("up_to_response_id") == (
        "checkout-response" if entry == "message" else None
    )
    assert chat.session_patches == []
