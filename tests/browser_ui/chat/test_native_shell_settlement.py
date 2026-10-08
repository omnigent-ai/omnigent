"""Exercise shell settlement in the built SPA with controlled backend events."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item

_USER = '[data-testid="message-bubble"][data-role="user"]'


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_shell_mirror_settles_its_bubble_before_the_next_prompt(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
    width: int,
) -> None:
    """A command card replaces its optimistic bubble across live and reload views."""
    chat = chat_session_contract
    chat.harness = "claude-native"
    chat.event_ack = {"queued": True, "pending_id": "pending-shell"}
    artifacts = Path(output_path)
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(chat.url)
    chat.wait_for_stream()

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=20_000)
    composer.fill("!echo shell-settled")
    with page.expect_response(
        lambda response: response.url.endswith(f"/{chat.session_id}/events")
    ):
        page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_USER)).to_have_count(1)
    page.screenshot(path=str(artifacts / "shell-pending.png"), full_page=True)

    shell_input = {
        "id": "shell-input",
        "response_id": "shell-turn",
        "type": "terminal_command",
        "kind": "input",
        "input": "echo shell-settled",
    }
    shell_output = {
        "id": "shell-output",
        "response_id": "shell-turn",
        "type": "terminal_command",
        "kind": "output",
        "stdout": "shell-settled\n",
    }
    for item in (shell_input, shell_output):
        chat.emit({"event": "response.output_item.done", "data": {"item": item}})
    shell = page.locator('[data-testid="terminal-command-card"][data-terminal-kind="input"]')
    expect(shell).to_have_count(1)
    expect(shell).to_contain_text("echo shell-settled")
    expect(page.locator(_USER)).to_have_count(0)
    output = page.locator('[data-testid="terminal-command-card"][data-terminal-kind="output"]')
    expect(output).to_have_count(1)
    output.click()
    expect(page.get_by_text("shell-settled", exact=True)).to_be_visible()
    chat.emit_idle(None)

    chat.event_ack = {"queued": True, "pending_id": "pending-next"}
    prompt = "Reply with prompt-settled"
    composer.fill(prompt)
    with page.expect_response(
        lambda response: response.url.endswith(f"/{chat.session_id}/events")
    ):
        page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_USER)).to_have_count(1)
    expect(page.locator(_USER)).to_contain_text(prompt)
    user = message_item("next-user", "user", prompt, response_id="next-turn")
    assistant = message_item(
        "next-assistant", "assistant", "prompt-settled", response_id="next-turn"
    )
    chat.emit_busy("next-turn")
    chat.emit(
        {
            "event": "session.input.consumed",
            "data": {
                "type": "session.input.consumed",
                "data": {
                    "item_id": user["id"],
                    "type": "message",
                    "cleared_pending_id": "pending-next",
                    "data": {"role": "user", "content": user["content"], "user_authored": True},
                },
            },
        }
    )
    chat.emit({"event": "response.output_item.done", "data": {"item": assistant}})
    chat.emit_idle("next-turn")
    expect(page.locator(_USER)).to_have_count(1)
    expect(page.get_by_text("prompt-settled", exact=True)).to_be_visible()
    expect(page.get_by_test_id("working-indicator")).to_have_count(0)
    assert len(chat.event_posts) == 2
    page.screenshot(path=str(artifacts / "shell-settled.png"), full_page=True)

    chat.set_items([assistant, user, shell_output, shell_input])
    chat.update_session(pending_inputs=[])
    page.reload()
    chat.wait_for_stream()
    expect(page.locator(_USER)).to_have_count(1)
    expect(page.locator(_USER)).to_contain_text(prompt)
    expect(shell).to_have_count(1)
    expect(page.get_by_text("prompt-settled", exact=True)).to_be_visible()
