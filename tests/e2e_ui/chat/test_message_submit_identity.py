"""UI journey: logical submit identity prevents replayed duplicate messages."""

from __future__ import annotations

import uuid
from typing import Any

from playwright.sync_api import Page, Request, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'


def test_replayed_submit_is_once_but_intentional_repeat_is_twice(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    text = f"repeatable-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "first accepted"}, {"text": "second accepted"}],
        key="submit-identity",
        match=text,
    )
    posted_events: list[dict[str, Any]] = []

    def capture_message(request: Request) -> None:
        if request.method != "POST" or not request.url.endswith(
            f"/sessions/{session_id}/events"
        ):
            return
        body = request.post_data_json
        if isinstance(body, dict) and body.get("type") == "message":
            posted_events.append(body)

    page.on("request", capture_message)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()

    composer.fill(text)
    composer.press("Enter")
    expect(page.locator(_ASSISTANT, has_text="first accepted")).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    assert len(posted_events) == 1
    first_event = posted_events[0]

    # Replay the exact browser request as though the server accepted it but the
    # HTTP response was lost before the client observed it.
    replay = page.evaluate(
        """async ({sessionId, event}) => {
          const response = await fetch(`/v1/sessions/${sessionId}/events`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(event),
          });
          return {status: response.status, body: await response.json()};
        }""",
        {"sessionId": session_id, "event": first_event},
    )
    assert replay["status"] == 202
    replayed_items = page.evaluate(
        """async (sessionId) => {
          const response = await fetch(`/v1/sessions/${sessionId}/items`);
          return (await response.json()).data;
        }""",
        session_id,
    )
    matching_replayed_items = [
        item
        for item in replayed_items
        if item.get("type") == "message"
        and item.get("role") == "user"
        and item.get("content", [{}])[0].get("text") == text
    ]
    assert len(matching_replayed_items) == 1, (
        "replaying a logical submit after a lost accepted response dispatched it twice"
    )
    assert isinstance(first_event.get("client_event_id"), str)
    assert replay["body"]["idempotency_replayed"] is True
    expect(page.locator('[data-testid="message-bubble"][data-role="user"]')).to_have_count(1)

    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT, has_text="second accepted")).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    message_events = [
        event for event in posted_events if event.get("data", {}).get("role") == "user"
    ]
    assert len(message_events) == 3
    assert message_events[0]["client_event_id"] == message_events[1]["client_event_id"]
    assert message_events[2]["client_event_id"] != message_events[0]["client_event_id"]

    items = page.evaluate(
        """async (sessionId) => {
          const response = await fetch(`/v1/sessions/${sessionId}/items`);
          return (await response.json()).data;
        }""",
        session_id,
    )
    matching_user_items = [
        item
        for item in items
        if item.get("type") == "message"
        and item.get("role") == "user"
        and item.get("content", [{}])[0].get("text") == text
    ]
    assert len(matching_user_items) == 2
