"""Cmd/Ctrl+Alt+U toggles the open session's unread flag from the composer."""

from __future__ import annotations

import json
from collections.abc import Callable

from playwright.sync_api import Page, Request, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract


def _read_state_put(session_id: str, *, unread: bool) -> Callable[[Request], bool]:
    """Match the PUT setting ``unread``, not the automatic one sent on viewing."""

    def matches(request: Request) -> bool:
        return (
            request.method == "PUT"
            and request.url.endswith(f"/v1/sessions/{session_id}/read-state")
            and json.loads(request.post_data or "{}").get("unread") is unread
        )

    return matches


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

    with page.expect_request(_read_state_put(chat.session_id, unread=True)):
        page.keyboard.press("ControlOrMeta+Alt+KeyU")
    expect(toasts.filter(has_text="Marked as unread")).to_be_visible()
    expect(unread_dot).to_be_visible()

    with page.expect_request(_read_state_put(chat.session_id, unread=False)):
        page.keyboard.press("ControlOrMeta+Alt+KeyU")
    expect(toasts.filter(has_text="Marked as read")).to_be_visible()
    expect(unread_dot).to_have_count(0)
