"""Bulk steering preserves queued messages for an explicit retry after failure."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, Route, expect


@pytest.mark.parametrize("alternate_send", [False, True], ids=["enter-send", "mod-enter-send"])
def test_bulk_steer_retries_the_whole_queue(
    page: Page,
    seeded_session: tuple[str, str],
    alternate_send: bool,
) -> None:
    """Exercise newline, normal send, bulk steering, and retry through the real composer."""
    base_url, session_id = seeded_session
    page.add_init_script(
        "localStorage.setItem('omnigent:composer-submit-with-mod-enter', "
        f"{json.dumps(str(alternate_send).lower())});"
    )
    posted: list[tuple[str, str]] = []
    fail_posts = True

    def respond_to_send(route: Route) -> None:
        data = route.request.post_data_json["data"]
        text = next(block["text"] for block in data["content"] if block["type"] == "input_text")
        posted.append((text, data["stable_id"]))
        # The initial ack leaves the local turn busy without invoking a model.
        failed = fail_posts and len(posted) > 1
        route.fulfill(
            status=503 if failed else 200,
            content_type="application/json",
            body=json.dumps(
                {"detail": "temporarily unavailable"}
                if failed
                else {"queued": True, "item_id": f"ci_bulk_{len(posted)}"}
            ),
        )

    page.route("**/v1/sessions/*/events", respond_to_send)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("first line")
    composer.press("Enter" if alternate_send else "Shift+Enter")
    composer.press_sequentially("second line")
    expect(composer).to_have_value("first line\nsecond line")
    assert not posted

    composer.fill("Keep this turn open.")
    send = page.get_by_role("button", name="Send", exact=True)
    with page.expect_response(lambda response: response.url.endswith(f"/{session_id}/events")):
        send.click()

    messages = ["queued first", "queued second", "draft last"]
    composer.fill(messages[0])
    send.click()
    composer.fill(messages[1])
    if alternate_send:
        composer.press("Control+Enter")
    else:
        send.click()
    strip = page.get_by_test_id("composer-queued-strip")
    expect(strip.get_by_text(messages[1], exact=True)).to_be_visible()
    assert len(posted) == 1

    chord = "Control+Shift+Enter" if alternate_send else "Control+Enter"
    composer.fill(messages[2])
    composer.press(chord)
    expect(strip.get_by_text("Send failed", exact=True)).to_have_count(3)
    expect(strip.get_by_role("button", name="Retry queued message", exact=True)).to_have_count(3)
    expect(composer).to_have_value("")
    for text in messages:
        expect(strip.get_by_text(text, exact=True)).to_be_visible()
    failed_posts = posted[1:]
    assert [text for text, _ in failed_posts] == messages

    fail_posts = False
    with page.expect_response(
        lambda response: (
            response.url.endswith(f"/{session_id}/events")
            and response.request.post_data_json["data"]["content"][0]["text"] == messages[-1]
        )
    ):
        composer.press(chord)
    expect(strip).to_have_count(0)
    assert posted[4:] == failed_posts, "Retry must keep message order and stable IDs"
