"""Undo in the message box after an @-mention menu edits the draft.

jsdom has no native undo history, so these run in a real browser.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator, Page, expect

from tests.browser_ui.chat._geometry_helpers import DESKTOP, open_live
from tests.browser_ui.chat.session_contract import ChatSessionContract
from tests.browser_ui.chat.test_composer_menu_ghosting import _stub_workspace_files
from tests.browser_ui.chat.test_native_start_picker import _open_picker


def test_undo_reverts_a_mention_pick(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    chat = chat_session_contract
    chat.harness = "claude-native"
    _stub_workspace_files(chat, ["report.md"])
    open_live(page, chat, DESKTOP)
    composer = page.get_by_label("Message the agent")
    composer.click()
    composer.press_sequentially("alpha bravo @rep")
    expect(page.get_by_role("option", name=re.compile(r"report\.md"))).to_be_visible()
    composer.press("Tab")
    expect(composer).to_have_value("alpha bravo @report.md ")

    composer.press("ControlOrMeta+z")
    expect(composer).to_have_value("alpha bravo @rep")


def _listing(*entries: tuple[str, str]) -> dict:
    return {
        "available": True,
        "data": [
            {"path": path, "name": path.rsplit("/", 1)[-1], "type": kind, "bytes": None}
            for path, kind in entries
        ],
        "has_more": False,
    }


def _session_composer(page: Page, chat: ChatSessionContract) -> Locator:
    chat.harness = "claude-native"
    open_live(page, chat, DESKTOP)
    composer = page.get_by_label("Message the agent")
    composer.click()
    return composer


def test_undo_reverts_a_folder_drill_down(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    chat = chat_session_contract
    _stub_workspace_files(chat, [])
    environment = f"/v1/sessions/{chat.session_id}/resources/environments/default"
    chat.contract.json(f"{environment}/filesystem", _listing(("docs", "directory")))
    chat.contract.json(f"{environment}/filesystem/docs", _listing(("docs/guide.md", "file")))
    composer = _session_composer(page, chat)
    composer.press_sequentially("alpha bravo @do")
    expect(page.get_by_title("Open docs")).to_be_visible()
    composer.press("Enter")
    expect(composer).to_have_value("alpha bravo @docs/")
    expect(page.get_by_title("Attach guide.md")).to_be_visible()

    composer.press("ControlOrMeta+z")
    expect(composer).to_have_value("alpha bravo @do")


def test_undo_restores_a_token_deleted_with_backspace(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    chat = chat_session_contract
    _stub_workspace_files(chat, ["report.md"])
    composer = _session_composer(page, chat)
    composer.press_sequentially("alpha bravo @rep")
    expect(page.get_by_title("Attach report.md")).to_be_visible()
    composer.press("Tab")
    expect(composer).to_have_value("alpha bravo @report.md ")
    composer.press("Backspace")
    composer.press("Backspace")
    expect(composer).to_have_value("alpha bravo ")

    composer.press("ControlOrMeta+z")
    expect(composer).to_have_value("alpha bravo @report.md")


def test_undo_reverts_a_clicked_mention_pick_in_the_launcher(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    chat = chat_session_contract
    chat.contract.json(
        re.compile(rf"/v1/hosts/{re.escape(chat.host_id)}/filesystem/work/repo(?:\?.*)?$"),
        {
            "object": "list",
            "data": [{"name": "report.md", "path": "/work/repo/report.md", "type": "file"}],
            "has_more": False,
        },
    )
    _open_picker(page, chat, "claude-native")
    composer = page.get_by_test_id("new-chat-landing-input")
    composer.click()
    composer.press_sequentially("alpha bravo @rep")
    page.get_by_title("Attach report.md").click()
    expect(composer).to_have_value("alpha bravo @report.md ")

    composer.press("ControlOrMeta+z")
    expect(composer).to_have_value("alpha bravo @rep")
