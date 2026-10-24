"""Undo must survive an ``@``-mention attach; native undo needs a real browser textarea."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract
from tests.browser_ui.chat.test_composer_menu_ghosting import _stub_workspace_files
from tests.browser_ui.chat.test_native_start_picker import _open_picker

_FILE_NAME = "report.md"
_TYPED = "alpha bravo"
_TOKEN = " @rep"
_GESTURES = ["Tab", "Enter", "click"]


def _attach_then_undo(page: Page, composer: Locator, gesture: str) -> None:
    composer.click()
    composer.press_sequentially(_TYPED + _TOKEN)
    first_item = page.get_by_test_id("file-mention-item-0")
    expect(first_item).to_contain_text(_FILE_NAME)
    if gesture == "click":
        first_item.get_by_title(f"Attach {_FILE_NAME}").click()
    else:
        composer.press(gesture)
    expect(page.get_by_role("button", name=re.compile(f"Remove .*{_FILE_NAME}"))).to_be_visible()
    expect(composer).to_have_value(f"{_TYPED} ")
    composer.press("ControlOrMeta+z")
    expect(composer).to_have_value(_TYPED + _TOKEN)


@pytest.mark.parametrize("gesture", _GESTURES)
def test_session_composer_undo_restores_mention_token(
    page: Page, chat_session_contract: ChatSessionContract, gesture: str
) -> None:
    chat = chat_session_contract
    chat.harness = "claude-native"
    _stub_workspace_files(chat, [_FILE_NAME])
    page.goto(chat.url)
    _attach_then_undo(page, page.get_by_label("Message the agent"), gesture)


@pytest.mark.parametrize("gesture", _GESTURES)
def test_launcher_undo_restores_mention_token(
    page: Page, chat_session_contract: ChatSessionContract, gesture: str
) -> None:
    chat = chat_session_contract
    chat.contract.json(
        re.compile(rf"/v1/hosts/{re.escape(chat.host_id)}/filesystem/work/repo(?:\?.*)?$"),
        {
            "object": "list",
            "data": [
                {
                    "name": _FILE_NAME,
                    "path": f"/work/repo/{_FILE_NAME}",
                    "type": "file",
                    "bytes": 10,
                    "modified_at": 0,
                }
            ],
            "has_more": False,
        },
    )
    _open_picker(page, chat, "claude-native")
    _attach_then_undo(page, page.get_by_test_id("new-chat-landing-input"), gesture)


def test_session_composer_undo_reverts_folder_drill_down(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    chat = chat_session_contract
    chat.harness = "claude-native"
    _stub_workspace_files(chat, [])
    environment = f"/v1/sessions/{chat.session_id}/resources/environments/default"
    chat.contract.json(
        f"{environment}/filesystem",
        {
            "available": True,
            "data": [
                {
                    "path": "docs",
                    "name": "docs",
                    "type": "directory",
                    "bytes": None,
                    "modified_at": None,
                }
            ],
            "has_more": False,
        },
    )
    chat.contract.json(
        f"{environment}/filesystem/docs",
        {
            "available": True,
            "data": [
                {
                    "path": "docs/guide.md",
                    "name": "guide.md",
                    "type": "file",
                    "bytes": 10,
                    "modified_at": None,
                }
            ],
            "has_more": False,
        },
    )
    page.goto(chat.url)
    composer = page.get_by_label("Message the agent")
    composer.click()
    composer.press_sequentially(f"{_TYPED} @do")
    expect(page.get_by_test_id("file-mention-item-0")).to_contain_text("docs")
    composer.press("Enter")
    expect(composer).to_have_value(f"{_TYPED} @docs/")
    expect(page.get_by_test_id("file-mention-item-0")).to_contain_text("guide.md")
    composer.press("ControlOrMeta+z")
    expect(composer).to_have_value(f"{_TYPED} @do")
