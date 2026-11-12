"""Browser-lane coverage for the browser tab title of the open chat."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, list_payload


def test_tab_title_uses_open_session_missing_from_sidebar_rows(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """A shared session outside the viewer's sidebar list titles the tab like the header."""
    chat = chat_session_contract
    title = "Quarterly release checklist"
    chat.update_session(title=title, permission_level=1)
    chat.contract.json("/v1/sessions", list_payload([]))

    page.goto(chat.url)
    expect(page.get_by_label("Message the agent")).to_be_disabled(timeout=20_000)
    breadcrumb = page.get_by_role("navigation", name="Conversation")
    expect(breadcrumb.get_by_text(title, exact=True)).to_be_visible()
    expect(page).to_have_title(title)
