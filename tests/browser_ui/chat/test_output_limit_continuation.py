"""A capped response can be continued explicitly without replaying its input."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    install_chat_session_routes,
    list_payload,
    message_item,
)


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
@pytest.mark.parametrize("surface", ["conversation", "canvas", "side-chat"])
def test_continue_output_limited_response(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
    width: int,
    surface: str,
) -> None:
    chat = chat_session_contract
    page.set_viewport_size({"width": width, "height": 900})
    chat.set_catalog(harness="claude-native", models=[], selected_model="")
    chat.update_session(title="Output limit recovery", status="failed")
    chat.set_items(
        [
            {
                "id": "output-limit-error",
                "type": "error",
                "response_id": "capped-response",
                "status": "completed",
                "source": "execution",
                "code": "max_output_tokens",
                "message": "API Error: Claude's response exceeded the 64000 output token maximum.",
            },
            message_item(
                "partial-answer",
                "assistant",
                "The first part is already saved.",
                response_id="capped-response",
            ),
            message_item("original-input", "user", "Write a detailed guide.", response_id="input"),
        ]
    )
    if surface == "canvas":
        chat.contract.json(
            "/v1/info",
            {
                "accounts_enabled": False,
                "single_user": True,
                "needs_setup": False,
                "features": {"canvas": True, "harness_install": False},
            },
        )
        page.goto(f"{chat.base_url}/canvas/c/{chat.session_id}")
    elif surface == "side-chat":
        parent = ChatSessionContract(
            page=page,
            contract=chat.contract,
            stream=chat.stream,
            session_id="browser-output-parent",
            harness="claude-native",
            models=[],
            selected_model="",
        )
        parent.update_session(title="Unaffected parent")
        install_chat_session_routes(parent)
        chat.update_session(
            labels={"omnigent.side_chat": "1", "omnigent.side_chat.source_id": parent.session_id}
        )
        chat.contract.json("/v1/sessions", list_payload([parent._session(), chat._session()]))
        chat.contract.json(f"/v1/sessions/{parent.session_id}/policies", list_payload([]))
        chat.contract.json("/v1/policy-registry", list_payload([]))
        chat.contract.json(f"/v1/sessions/{parent.session_id}/owner", {"owner": None})
        state = {
            "id": parent.session_id,
            "state": {
                "open": True,
                "rightRailTab": "sidechat",
                "openSideChats": [chat.session_id],
                "selectedSideChatId": chat.session_id,
            },
        }
        page.add_init_script(
            "localStorage.setItem('omnigent:session-workspace-state', "
            f"JSON.stringify([{json.dumps(state)}]));"
            f"localStorage.setItem('omnigent.sideChatInherited:{chat.session_id}', '[]');"
        )
        page.goto(parent.url)
        if width < 768:
            page.get_by_role("button", name="Conversation actions", exact=True).click()
            page.get_by_role("menuitem", name="Side chats", exact=True).click()
        else:
            toggle = page.get_by_role("button", name=re.compile("right panel", re.I))
            if toggle.get_attribute("aria-label") == "Expand right panel":
                toggle.click()
            page.get_by_role("tab", name="Side chat 1", exact=True).click()
    else:
        page.goto(chat.url)

    partial = page.get_by_text("The first part is already saved.", exact=True)
    expect(partial).to_be_visible()
    action = page.get_by_role("button", name="Continue", exact=True)
    expect(action).to_be_visible()
    artifacts = Path(output_path)
    page.screenshot(path=str(artifacts / "output-limit-continue.png"), full_page=True)
    action.click()
    expect(action).to_have_count(0)
    expect(partial).to_be_visible()
    assert len(chat.event_posts) == 1
    assert chat.event_posts[0]["body"] == {
        "type": "message",
        "data": {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Please continue from where you left off."}
            ],
        },
    }
    page.screenshot(path=str(artifacts / "output-limit-continued.png"), full_page=True)
