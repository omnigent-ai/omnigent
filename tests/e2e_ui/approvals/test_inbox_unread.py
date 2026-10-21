"""E2E: live agent replies appear in the Inbox as unread rows.

Verifies the tabs filter them, the selected tab persists across reloads, and
"Mark as read" and "Open session" both clear the row.
"""

from __future__ import annotations

import re
import time
import uuid

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_UNREAD_ROW = '[data-testid="inbox-unread"]'
_FIRST_REPLY = "Refactor finished: 3 files changed, all tests green."
_SECOND_REPLY = "Follow-up done: added the missing migration."
_TURN_TIMEOUT_MS = 60_000


def _send_user_message(base_url: str, session_id: str, text: str) -> None:
    """Start a turn through the events API, as another client would.

    :param base_url: Live server base URL.
    :param session_id: Session to message.
    :param text: User message text.
    """
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        timeout=30.0,
    ).raise_for_status()


def _wait_for_next_second() -> None:
    """Let ``updated_at`` (whole seconds) move past the read-as-of-load baseline."""
    time.sleep(1.1)


def test_unseen_reply_surfaces_in_inbox_until_read(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A reply that lands off-screen is listed as unread, filtered, and cleared."""
    base_url, session_id = seeded_session
    first_marker = f"inbox-unread-{uuid.uuid4().hex[:8]}"
    second_marker = f"inbox-unread-{uuid.uuid4().hex[:8]}"
    # Several copies per marker so a title-generation call matching the same
    # prompt can't drain the queue before the agent's turn.
    configure_mock_llm(mock_llm_server_url, [{"text": _FIRST_REPLY}] * 3, match=first_marker)
    configure_mock_llm(mock_llm_server_url, [{"text": _SECOND_REPLY}] * 3, match=second_marker)

    page.goto(f"{base_url}/inbox")
    expect(page.get_by_text("Nothing waiting on you")).to_be_visible(timeout=30_000)

    _wait_for_next_second()
    _send_user_message(base_url, session_id, f"Summarize the refactor. Marker: {first_marker}")

    row = page.locator(_UNREAD_ROW)
    expect(row).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(row).to_have_attribute("data-kind", "done")
    expect(row).to_contain_text(_FIRST_REPLY, timeout=15_000)
    expect(page.get_by_title("1 unread")).to_be_visible()

    # It raised no approval, so "Awaiting response" leaves it out.
    page.get_by_role("tab", name="Awaiting response").click()
    expect(row).to_have_count(0)
    expect(page.get_by_text("No approvals waiting")).to_be_visible()
    page.get_by_role("tab", name="Unread").click()
    expect(row).to_be_visible()

    row.locator("button[aria-expanded]").click()
    expect(row).to_have_attribute("data-expanded", "true")
    row.get_by_role("button", name="Mark as read").click()
    expect(row).to_have_count(0)
    expect(page.get_by_text("You’re all caught up")).to_be_visible()

    # The tab choice and the read state both survive a reload.
    page.reload()
    expect(page.get_by_role("tab", name="Unread")).to_have_attribute(
        "aria-selected", "true", timeout=30_000
    )
    expect(page.get_by_text("You’re all caught up")).to_be_visible(timeout=30_000)
    expect(row).to_have_count(0)

    # A new reply re-lights the row; opening the session is reading it.
    _wait_for_next_second()
    _send_user_message(base_url, session_id, f"Add the migration too. Marker: {second_marker}")
    expect(row).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(row).to_contain_text(_SECOND_REPLY, timeout=15_000)
    row.locator("button[aria-expanded]").click()
    row.get_by_role("link", name="Open session").click()
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(session_id)}"))
    expect(
        page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
            has_text=_SECOND_REPLY
        )
    ).to_be_visible(timeout=30_000)

    page.locator('a[href="/inbox"]').first.click()
    expect(page).to_have_url(re.compile(r"/inbox$"))
    expect(page.get_by_text("You’re all caught up")).to_be_visible(timeout=30_000)
    expect(row).to_have_count(0)
