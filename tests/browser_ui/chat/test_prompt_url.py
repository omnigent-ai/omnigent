"""Prompt links populate the built composer's draft before explicit submission."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract
from tests.browser_ui.chat.test_native_start_picker import _open_picker


def test_prompt_link_is_reviewable_before_starting_a_session(
    page: Page, chat_session_contract: ChatSessionContract, tmp_path: Path
) -> None:
    chat = chat_session_contract
    creates = _open_picker(page, chat, "pi-native")
    prompt = 'src/example.py\nExplain a + b & suggest a fix for "λ".'
    query = urlencode({"prompt": prompt, "o": "123"})

    page.goto(f"{chat.base_url}/?{query}#composer")
    composer = page.get_by_test_id("new-chat-landing-input")
    expect(composer).to_have_value(prompt)
    expect(page).to_have_url(f"{chat.base_url}/?o=123#composer")
    expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
    assert creates == []
    assert chat.event_posts == []
    page.screenshot(path=tmp_path / "prefilled-prompt.png")

    composer.fill(prompt + "\nKeep the public API unchanged.")
    expect(composer).to_have_value(prompt + "\nKeep the public API unchanged.")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/sessions")
        )
    ):
        page.get_by_test_id("new-chat-landing-submit").click()
    assert len(creates) == 1
