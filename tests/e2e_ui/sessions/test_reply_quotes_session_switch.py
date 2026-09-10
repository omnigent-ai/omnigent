"""Reply quotes must not follow the composer into another session."""

from pathlib import Path

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn


def test_reply_quotes_clear_when_switching_sessions(
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

    for _ in range(2):
        assistant.evaluate("""element => {
            const range = document.createRange();
            range.selectNodeContents(element);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            document.dispatchEvent(new Event("selectionchange"));
        }""")
        page.get_by_role("button", name="Reply", exact=False).click()

    remove_quote = page.get_by_role("button", name="Remove quote", exact=True)
    expect(remove_quote).to_have_count(2)
    composer.fill("Unsent draft in the original session")
    expect(remove_quote).to_have_count(2)
    page.screenshot(path=str(tmp_path / "reply-before-switch.png"))

    page.locator(f'a[href="/c/{session_b}"]').first.click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}")
    expect(composer).to_have_value("")
    expect(remove_quote).to_have_count(0)
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
    expect(composer).to_have_value("Unsent draft in the original session")
    expect(remove_quote).to_have_count(0)
