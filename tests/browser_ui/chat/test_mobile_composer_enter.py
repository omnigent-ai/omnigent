"""Browser-only touch-viewport contract for composer Enter behavior."""

import pytest
from playwright.sync_api import Page, expect


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 390, "height": 844},
        "has_touch": True,
        "is_mobile": True,
    }


def test_mobile_enter_inserts_newline_and_send_submits_once(
    page: Page, chat_session_contract
) -> None:
    page.goto(chat_session_contract.url)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()
    assert page.evaluate("matchMedia('(pointer: coarse)').matches")

    composer.fill("first line")
    composer.press("Enter")
    composer.type("second line")

    prompt = "first line\nsecond line"
    expect(composer).to_have_value(prompt)
    assert chat_session_contract.event_posts == []

    page.get_by_role("button", name="Send", exact=True).click()
    expect(composer).to_have_value("")
    expect(
        page.locator('[data-testid="message-bubble"][data-role="user"]', has_text="first line")
    ).to_have_count(1)
    assert len(chat_session_contract.event_posts) == 1
    event = chat_session_contract.event_posts[0]["body"]
    assert event["type"] == "message"
    assert event["data"]["content"] == [{"type": "input_text", "text": prompt}]
