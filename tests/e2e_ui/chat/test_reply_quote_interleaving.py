"""Reply quotes must interleave with typed answers, not group above the draft.

Journey: quote one passage of an assistant reply with the floating
"Reply" button, type an answer, quote a second passage, type a second
answer, and send. The message must preserve the user's interleaved
order (quote, answer, quote, answer); a composer that keeps quotes in
a separate list above the textarea and prepends them all on send
produces quote, quote, answer, answer instead.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, seed_committed_turn

_PARA_1 = "The moon has no atmosphere to scatter sunlight."
_PARA_2 = "Lunar surface temperatures swing by hundreds of degrees."
_ANSWER_1 = "Answer to the first passage: that explains the stark shadows."
_ANSWER_2 = "Answer to the second passage: so landings target lunar dawn."


def _quote_passage(page: Page, passage: str) -> None:
    """Select *passage* inside the assistant message and click Reply."""
    target = page.locator('[data-role="assistant"]').get_by_text(passage, exact=True)
    expect(target).to_be_visible()
    target.evaluate(
        """element => {
        const range = document.createRange();
        range.selectNodeContents(element);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        document.dispatchEvent(new Event("selectionchange"));
    }"""
    )
    page.get_by_role("button", name="Reply", exact=False).click()


def test_reply_quotes_interleave_with_typed_answers(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    seed_committed_turn(
        session_id,
        prompt="Tell me two facts about the moon",
        reply=f"{_PARA_1}\n\n{_PARA_2}",
    )
    # The sent draft reaches the mock LLM as one message; ack it so the
    # turn settles instead of erroring after the send this test asserts on.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Acknowledged."}],
        key="quote-interleave",
        match=_ANSWER_2,
    )

    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()

    # Quote passage 1, answer it, then quote passage 2 and answer that too —
    # the user drafts each answer under the passage it answers.
    _quote_passage(page, _PARA_1)
    remove_quote = page.get_by_role("button", name="Remove quote", exact=True)
    expect(remove_quote).to_have_count(1)
    composer.fill(_ANSWER_1)

    _quote_passage(page, _PARA_2)
    expect(remove_quote).to_have_count(2)
    composer.fill(f"{_ANSWER_1}\n\n{_ANSWER_2}")

    events_url = f"{base_url}/v1/sessions/{session_id}/events"
    with page.expect_request(events_url) as sent_request:
        page.get_by_role("button", name="Send", exact=True).click()

    # Let the sent draft render as a user bubble before asserting, so a
    # failing run still shows the user-visible outcome in the transcript.
    sent_bubble = page.locator(
        '[data-testid="message-bubble"][data-role="user"]', has_text=_ANSWER_2
    )
    expect(sent_bubble).to_be_visible(timeout=15_000)

    content = sent_request.value.post_data_json["data"]["content"]
    assert content and content[0]["type"] == "input_text"
    text = content[0]["text"]

    positions = {
        "first quote": text.find(f"> {_PARA_1}"),
        "first answer": text.find(_ANSWER_1),
        "second quote": text.find(f"> {_PARA_2}"),
        "second answer": text.find(_ANSWER_2),
    }
    missing = [name for name, pos in positions.items() if pos < 0]
    assert not missing, f"sent message lost segments {missing}: {text!r}"

    ordered = sorted(positions, key=positions.__getitem__)
    assert ordered == ["first quote", "first answer", "second quote", "second answer"], (
        "reply quotes were grouped above the draft instead of interleaved "
        f"with the typed answers — sent order {ordered}: {text!r}"
    )

    # Post-fix the turn completes: the mock LLM acks the interleaved reply.
    expect(
        page.locator(
            '[data-testid="message-bubble"][data-role="assistant"]', has_text="Acknowledged."
        )
    ).to_be_visible(timeout=60_000)
