"""Browser-lane coverage for live status events reaching chat indicators."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, session_status_event


def test_bare_idle_clears_the_live_working_indicator(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """The real SPA consumes a running edge followed by an id-less idle."""
    chat = chat_session_contract
    page.goto(chat.url)
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=20_000)
    chat.wait_for_stream()
    working = page.get_by_test_id("working-indicator")

    chat.emit_busy("browser-turn")
    expect(working).to_be_visible(timeout=10_000)

    chat.emit_idle(None)
    expect(working).to_be_hidden(timeout=10_000)


def test_blocked_reason_reaches_the_live_working_indicator(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """A parked native turn names its reason and clears it on the next edge."""
    chat = chat_session_contract
    page.goto(chat.url)
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=20_000)
    chat.wait_for_stream()
    working = page.get_by_test_id("working-indicator")

    chat.emit(
        {
            "event": "session.status",
            "data": {
                "conversation_id": chat.session_id,
                "status": "running",
                "blocked_on": "permission prompt",
            },
        }
    )
    expect(working).to_contain_text("Blocked on: permission prompt", timeout=10_000)

    chat.emit(session_status_event(chat.session_id, "running"))
    expect(working).not_to_contain_text("Blocked on:", timeout=10_000)

    chat.emit_idle(None)
    expect(working).to_be_hidden(timeout=10_000)
