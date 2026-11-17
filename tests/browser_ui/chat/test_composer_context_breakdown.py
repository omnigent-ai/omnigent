"""The composer's `/context` command opens the context ring's breakdown."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract


def test_slash_context_opens_the_runner_breakdown(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """`/context` opens the ring's popover with the runner's rows and a failed attempt."""
    chat = chat_session_contract
    chat.seed_transcript(2)
    chat.update_session(context_window=200_000, last_total_tokens=48_200)
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/context-breakdown",
        {
            "session_id": chat.session_id,
            "system_prompt_tokens": 2_100,
            "tools": {"mcp_servers": ["github"]},
            "attachments": [],
            "history": {"estimated_tokens": 21_000, "item_count": 4},
            "bytes_per_token": 4,
            "reported": {
                "system_tokens": 2_400,
                "tool_tokens": 11_800,
                "tool_count": 27,
                "history_tokens": 21_500,
                "history_item_count": 4,
                "message_tokens": 40,
            },
            "failed_attempt": {"history_tokens": 191_200, "history_item_count": 212},
        },
    )
    page.goto(chat.url)
    chat.wait_for_stream()
    expect(page.get_by_test_id("composer-context-ring")).to_be_visible()

    composer = page.get_by_role("textbox").first
    composer.fill("/context")
    composer.press("Enter")

    dialog = page.get_by_role("dialog", name="Context window breakdown")
    expect(dialog).to_be_visible()
    # The command clears itself instead of sending a message.
    expect(composer).to_have_value("")
    tools = dialog.get_by_test_id("context-row-tools")
    expect(tools).to_contain_text("~11.8K")
    expect(tools).to_contain_text("27 tools · MCP: github")
    expect(dialog.get_by_test_id("context-failed-attempt")).to_contain_text(
        "Last request was too long"
    )
    assert chat.event_posts == []
