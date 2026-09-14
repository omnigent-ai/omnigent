"""Reply quotes stay interleaved in each session's message draft."""

from pathlib import Path

from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import seed_committed_turn


def _reply_to(page: Page, text: Locator) -> None:
    text.evaluate("""element => {
        const range = document.createRange();
        range.selectNodeContents(element);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        document.dispatchEvent(new Event("selectionchange"));
    }""")
    page.get_by_role("button", name="Reply", exact=False).click()


def test_reply_quotes_append_after_the_draft_and_send_interleaved(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    base_url, session_id = seeded_session
    first_quote = "First point to discuss."
    second_quote = "Second point to discuss."
    seed_committed_turn(
        session_id,
        prompt="Please explain both points.",
        reply=f"{first_quote}\n\n{second_quote}",
    )
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent")
    assistant = page.locator('[data-role="assistant"]')
    first_point = assistant.get_by_text(first_quote, exact=True)
    second_point = assistant.get_by_text(second_quote, exact=True)
    expect(first_point).to_be_visible()
    expect(second_point).to_be_visible()

    composer.fill("My introduction.")
    _reply_to(page, first_point)
    cards = page.get_by_test_id("composer-reply-quote")
    expect(cards).to_have_count(1)
    expect(cards.first.locator("blockquote")).to_have_text(first_quote)
    expect(cards.first.locator("blockquote")).to_have_css("border-left-width", "2px")
    expect(page.get_by_role("textbox", name="Reply text before quote 1")).to_have_value(
        "My introduction."
    )
    expect(composer).to_have_value("")
    expect(composer).to_be_focused()
    page.keyboard.insert_text("My first answer.")

    # Reply appends even if the composer selection is in earlier text.
    composer.evaluate("element => element.setSelectionRange(0, 2)")
    _reply_to(page, second_point)
    expected = f"My introduction.\n\n> {first_quote}\n\nMy first answer.\n\n> {second_quote}\n\n"
    expect(composer).to_have_value("")
    expect(cards).to_have_count(2)
    expect(cards.nth(1).locator("blockquote")).to_have_text(second_quote)
    earlier_reply = page.get_by_role("textbox", name="Reply text before quote 2")
    expect(earlier_reply).to_have_value("My first answer.")
    first_box = cards.first.bounding_box()
    reply_box = earlier_reply.bounding_box()
    second_box = cards.nth(1).bounding_box()
    assert first_box is not None and reply_box is not None and second_box is not None
    assert first_box["y"] < reply_box["y"] < second_box["y"]
    expect(composer).to_be_focused()
    page.keyboard.insert_text("My second answer.")
    expected += "My second answer."
    expect(composer).to_have_value("My second answer.")
    page.screenshot(path=str(tmp_path / "interleaved-reply-quotes.png"))

    events_url = f"{base_url}/v1/sessions/{session_id}/events"
    page.route(
        events_url,
        lambda route: route.fulfill(json={"queued": True, "item_id": "ci_interleaved_reply"}),
    )
    with page.expect_request(events_url) as sent_request:
        page.get_by_role("button", name="Send", exact=True).click()
    assert sent_request.value.post_data_json["data"]["content"] == [
        {"type": "input_text", "text": expected}
    ]
    expect(composer).to_have_value("")
    expect(cards).to_have_count(0)


def test_reply_quotes_stay_in_their_original_session(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
    tmp_path: Path,
) -> None:
    base_url, session_a, session_b = seeded_session_pair
    quoted_text = "This response belongs only to the original session."
    seed_committed_turn(session_a, prompt="Original question", reply=quoted_text)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_b}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    page.locator(f'a[href="/c/{session_a}"]').first.click()
    assistant = page.locator('[data-role="assistant"]').get_by_text(quoted_text, exact=True)
    expect(assistant).to_be_visible()

    _reply_to(page, assistant)
    page.keyboard.insert_text("Unsent draft in the original session")
    _reply_to(page, assistant)
    cards = page.get_by_test_id("composer-reply-quote")
    expect(cards).to_have_count(2)
    expect(composer).to_have_value("")
    expect(page.get_by_role("textbox", name="Reply text before quote 2")).to_have_value(
        "Unsent draft in the original session"
    )
    page.screenshot(path=str(tmp_path / "reply-before-switch.png"))

    page.locator(f'a[href="/c/{session_b}"]').first.click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}")
    expect(composer).to_have_value("")
    expect(cards).to_have_count(0)
    page.screenshot(path=str(tmp_path / "reply-after-switch.png"))

    prompt = "A fresh prompt for the other session"
    events_url = f"{base_url}/v1/sessions/{session_b}/events"
    page.route(
        events_url,
        lambda route: route.fulfill(json={"queued": True, "item_id": "ci_reply_switch"}),
    )
    composer.fill(prompt)
    with page.expect_request(events_url) as sent_request:
        page.get_by_role("button", name="Send", exact=True).click()
    assert sent_request.value.post_data_json["data"]["content"] == [
        {"type": "input_text", "text": prompt}
    ]

    page.locator(f'a[href="/c/{session_a}"]').first.click()
    expect(cards).to_have_count(2)
    expect(page.get_by_role("textbox", name="Reply text before quote 2")).to_have_value(
        "Unsent draft in the original session"
    )
    page.reload()
    expect(cards).to_have_count(2)
    expect(page.get_by_role("textbox", name="Reply text before quote 2")).to_have_value(
        "Unsent draft in the original session"
    )


def test_removing_quote_cards_preserves_edited_replies(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    seed_committed_turn(session_id, prompt="Two points", reply="First point.\n\nSecond point.")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent")
    assistant = page.locator('[data-role="assistant"]')
    first = assistant.get_by_text("First point.", exact=True)
    expect(first).to_be_visible()
    composer.fill("Introduction")
    _reply_to(page, first)
    page.keyboard.insert_text("First answer")
    _reply_to(page, assistant.get_by_text("Second point.", exact=True))
    page.keyboard.insert_text("Second answer")
    earlier = page.get_by_role("textbox", name="Reply text before quote 2")
    earlier.fill("")
    expect(earlier).to_be_focused()
    page.keyboard.insert_text("Rewritten answer")
    expect(earlier).to_have_value("Rewritten answer")

    remove_quote = page.get_by_role("button", name="Remove quote", exact=True)
    remove_quote.first.click()
    expect(remove_quote).to_have_count(1)
    expect(page.get_by_role("textbox", name="Reply text before quote 1")).to_have_value(
        "Introduction\n\nRewritten answer"
    )
    remove_quote.click()
    expect(remove_quote).to_have_count(0)
    expect(composer).to_have_value("Introduction\n\nRewritten answer\n\nSecond answer")


def test_authored_markdown_stays_editable_after_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent")
    text = "intro\n> quote\nreply\n\n> quoted\ncontinued"
    composer.fill(text)
    page.reload()
    expect(composer).to_have_value(text)
    expect(page.get_by_test_id("composer-reply-quote")).to_have_count(0)

    events_url = f"{base_url}/v1/sessions/{session_id}/events"
    page.route(events_url, lambda route: route.fulfill(json={"queued": True}))
    with page.expect_request(events_url) as sent:
        page.get_by_role("button", name="Send", exact=True).click()
    assert sent.value.post_data_json["data"]["content"] == [{"type": "input_text", "text": text}]


def test_reply_card_provenance_survives_reload_beside_authored_markdown(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    seed_committed_turn(session_id, prompt="Two points", reply="First point.\n\nSecond point.")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent")
    assistant = page.locator('[data-role="assistant"]')
    before = "Notes:\n> authored blockquote\nlazy continuation\n\n\n"
    middle = "~~~markdown\n> code example\n"
    tail = "> authored tail\ncontinued\n\n"
    composer.fill(before)
    _reply_to(page, assistant.get_by_text("First point.", exact=True))
    composer.fill(middle)
    _reply_to(page, assistant.get_by_text("Second point.", exact=True))
    composer.fill(tail)

    page.reload()
    cards = page.get_by_test_id("composer-reply-quote")
    expect(cards).to_have_count(2)
    expect(cards.first.locator("blockquote")).to_have_text("First point.")
    expect(cards.nth(1).locator("blockquote")).to_have_text("Second point.")
    expect(page.get_by_role("textbox", name="Reply text before quote 1")).to_have_value(before)
    expect(page.get_by_role("textbox", name="Reply text before quote 2")).to_have_value(middle)
    expect(composer).to_have_value(tail)

    page.get_by_role("button", name="Remove quote", exact=True).first.click()
    page.get_by_role("button", name="Remove quote", exact=True).click()
    expect(cards).to_have_count(0)
    expect(composer).to_have_value(before + middle + "\n" + tail)
