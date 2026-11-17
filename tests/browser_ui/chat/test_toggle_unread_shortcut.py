"""Cmd/Ctrl+Alt+U toggles the open session's unread flag from the composer."""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract


def test_shortcut_marks_the_open_session_unread_then_read(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """A real keydown in the focused composer lights and clears the row's unread dot."""
    chat = chat_session_contract
    page.goto(chat.url)
    row = page.locator(f'li[data-sidebar-session-id="{chat.session_id}"]')
    unread_dot = row.locator('[data-testid="session-state-badge"][data-state="unseen"]')
    toasts = page.locator("[data-sonner-toast]")
    expect(row).to_be_visible()
    expect(unread_dot).to_have_count(0)
    page.get_by_role("textbox", name="Message the agent").focus()

    read_state = f"/v1/sessions/{chat.session_id}/read-state"
    with page.expect_request(lambda request: request.url.endswith(read_state)) as marked:
        page.keyboard.press("ControlOrMeta+Alt+KeyU")
    assert json.loads(marked.value.post_data or "{}")["unread"] is True
    expect(toasts.filter(has_text="Marked as unread")).to_be_visible()
    expect(unread_dot).to_be_visible()

    with page.expect_request(lambda request: request.url.endswith(read_state)) as cleared:
        page.keyboard.press("ControlOrMeta+Alt+KeyU")
    assert json.loads(cleared.value.post_data or "{}")["unread"] is False
    expect(toasts.filter(has_text="Marked as read")).to_be_visible()
    expect(unread_dot).to_have_count(0)
