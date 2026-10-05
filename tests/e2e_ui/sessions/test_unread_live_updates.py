"""Deep browser coverage for live unread-state updates.

These journeys keep the sidebar mounted while a second client mutates the
session through the real REST event paths. They intentionally avoid reloads:
the unread decision must follow the session-updates stream and the live item
stream, while metadata and hidden context must not advance the visible-message
watermark.
"""

from __future__ import annotations

import uuid
from typing import Any

from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.sessions.test_sidebar_mark_unread import (
    _append_assistant_message,
    _row,
    _unread_dot,
)

_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING_INDICATOR = '[data-testid="working-indicator"]'


def _session(page: Page, base_url: str, session_id: str) -> dict[str, Any]:
    """Read one session snapshot for a read-only watermark cross-check."""
    response = page.request.get(f"{base_url}/v1/sessions/{session_id}")
    assert response.ok, response.text()
    return response.json()


def _list_item(page: Page, base_url: str, session_id: str) -> dict[str, Any]:
    """Read the list projection, which carries the visible-message watermark."""
    response = page.request.get(f"{base_url}/v1/sessions?visibility=all")
    assert response.ok, response.text()
    return next(item for item in response.json()["data"] if item["id"] == session_id)


def _items(page: Page, base_url: str, session_id: str) -> list[dict[str, Any]]:
    """Read the committed item list without changing browser state."""
    response = page.request.get(f"{base_url}/v1/sessions/{session_id}/items?limit=100&order=asc")
    assert response.ok, response.text()
    return response.json()["data"]


def _post_event(
    page: Page,
    base_url: str,
    session_id: str,
    event_type: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Post one supported external event through the browser's API context."""
    response = page.request.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        data={"type": event_type, "data": data},
    )
    assert response.ok, response.text()
    return response.json()


def _leave_chat(page: Page, base_url: str, session_id: str) -> Locator:
    """Leave the active chat and let its read watermark age past one tick."""
    page.get_by_test_id("inbox-button").click()
    expect(page).to_have_url(f"{base_url}/inbox")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    # Read-state uses epoch-second precision. This keeps a message posted
    # after the route transition strictly newer than the active-view watermark.
    page.wait_for_timeout(1_300)
    return row


def _seed_visible_message(page: Page, base_url: str, session_id: str, text: str) -> None:
    """Append a settled assistant item before opening the session."""
    _append_assistant_message(page, base_url, session_id, text)


def test_metadata_model_report_and_hidden_meta_stay_read_live(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Metadata and hidden context updates do not light an unread dot live."""
    base_url, session_id = seeded_session
    _seed_visible_message(page, base_url, session_id, "Existing visible answer.")

    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_unread_dot(row)).to_have_count(0)
    before_items = _items(page, base_url, session_id)
    before_last_message_at = _list_item(page, base_url, session_id)["last_message_at"]
    before_message_ids = {item["id"] for item in before_items if item.get("type") == "message"}

    row = _leave_chat(page, base_url, session_id)

    title = f"live-metadata-{uuid.uuid4().hex[:8]}"
    title_response = page.request.patch(
        f"{base_url}/v1/sessions/{session_id}",
        data={"title": title},
    )
    assert title_response.ok, title_response.text()
    # Title delivery is the live-stream proof for the metadata phase; a dead
    # stream would only update after the 45–60 second fallback reconcile.
    expect(row).to_contain_text(title, timeout=20_000)
    expect(_unread_dot(row)).to_have_count(0)

    model = f"reported-live-{uuid.uuid4().hex[:8]}"
    _post_event(
        page,
        base_url,
        session_id,
        "external_model_change",
        {"model": model},
    )
    model_snapshot = _session(page, base_url, session_id)
    assert model_snapshot["llm_model"] == model
    expect(_unread_dot(row)).to_have_count(0)

    hidden_text = f"hidden-context-{uuid.uuid4().hex[:8]}"
    hidden_response_id = f"resp-hidden-{uuid.uuid4().hex[:8]}"
    hidden_ack = _post_event(
        page,
        base_url,
        session_id,
        "external_conversation_item",
        {
            "item_type": "message",
            "response_id": hidden_response_id,
            "item_data": {
                "role": "user",
                "content": [{"type": "input_text", "text": hidden_text}],
                "is_meta": True,
            },
        },
    )
    hidden_item_id = hidden_ack["item_id"]
    hidden_item = next(
        item for item in _items(page, base_url, session_id) if item["id"] == hidden_item_id
    )
    assert hidden_item["is_meta"] is True
    assert hidden_text in str(hidden_item["content"])

    after = _list_item(page, base_url, session_id)
    after_message_ids = {
        item["id"] for item in _items(page, base_url, session_id) if item.get("type") == "message"
    }
    assert after["last_message_at"] == before_last_message_at
    assert after_message_ids == before_message_ids | {hidden_item_id}
    expect(_unread_dot(row)).to_have_count(0)


def test_visible_message_lights_dot_via_live_updates_and_reopen_clears_it(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A visible assistant event lights the row without a reload."""
    base_url, session_id = seeded_session
    _seed_visible_message(page, base_url, session_id, "Read before leaving.")

    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_unread_dot(row)).to_have_count(0)
    before_last_message_at = _list_item(page, base_url, session_id)["last_message_at"]
    _leave_chat(page, base_url, session_id)

    live_text = f"live-away-answer-{uuid.uuid4().hex[:8]}"
    _append_assistant_message(page, base_url, session_id, live_text)

    expect(_unread_dot(row)).to_be_visible(timeout=20_000)
    after = _list_item(page, base_url, session_id)
    assert after["last_message_at"] > before_last_message_at
    assert any(
        live_text in str(item.get("content")) for item in _items(page, base_url, session_id)
    )

    row.locator(f'a[href="/c/{session_id}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_id}")
    expect(_unread_dot(_row(page, session_id))).to_have_count(0)


def test_running_suppresses_live_message_until_idle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A live visible message waits for an idle status before lighting."""
    base_url, session_id = seeded_session
    _seed_visible_message(page, base_url, session_id, "Settled baseline.")

    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_unread_dot(row)).to_have_count(0)
    _leave_chat(page, base_url, session_id)

    response_id = f"resp-running-{uuid.uuid4().hex[:8]}"
    _post_event(
        page,
        base_url,
        session_id,
        "external_session_status",
        {"status": "running", "response_id": response_id},
    )
    running_badge = row.locator('[data-testid="session-state-badge"][data-state="running"]')
    expect(running_badge).to_be_visible(timeout=20_000)

    live_text = f"running-answer-{uuid.uuid4().hex[:8]}"
    _append_assistant_message(page, base_url, session_id, live_text)
    expect(_unread_dot(row)).to_have_count(0)
    assert any(
        live_text in str(item.get("content")) for item in _items(page, base_url, session_id)
    )

    _post_event(
        page,
        base_url,
        session_id,
        "external_session_status",
        {"status": "idle", "response_id": response_id},
    )
    expect(running_badge).to_have_count(0, timeout=20_000)
    expect(_unread_dot(row)).to_be_visible(timeout=20_000)


def test_focused_chat_receives_live_message_and_stays_read_after_leaving(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A focused chat marks a live assistant message read before leaving."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_unread_dot(row)).to_have_count(0)

    live_text = f"focused-answer-{uuid.uuid4().hex[:8]}"
    _append_assistant_message(page, base_url, session_id, live_text)
    expect(page.locator(_ASSISTANT_BUBBLE, has_text=live_text)).to_be_visible(timeout=20_000)
    expect(_unread_dot(row)).to_have_count(0)

    page.get_by_test_id("inbox-button").click()
    expect(page).to_have_url(f"{base_url}/inbox")
    expect(_row(page, session_id)).to_be_visible()
    expect(_unread_dot(_row(page, session_id))).to_have_count(0)


def test_real_composer_turn_then_live_followup_marks_unread(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A real composer turn is read, then a later live turn is unread."""
    base_url, session_id = seeded_session
    token = f"composer-live-{uuid.uuid4().hex[:8]}"
    reply = f"composer-reply-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": reply}],
        key=f"composer-live-{token}",
        match=token,
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    composer.fill(f"Please include this token exactly: {token}")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT_BUBBLE, has_text=reply)).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING_INDICATOR)).to_have_count(0, timeout=60_000)
    row = _row(page, session_id)
    expect(_unread_dot(row)).to_have_count(0)

    row = _leave_chat(page, base_url, session_id)
    followup = f"followup-away-{uuid.uuid4().hex[:8]}"
    _append_assistant_message(page, base_url, session_id, followup)
    expect(_unread_dot(row)).to_be_visible(timeout=20_000)

    row.locator(f'a[href="/c/{session_id}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_id}")
    expect(_unread_dot(_row(page, session_id))).to_have_count(0)


def test_empty_session_metadata_update_stays_read_live(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Metadata changes on an empty session do not create a false dot."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_unread_dot(row)).to_have_count(0)
    before = _list_item(page, base_url, session_id)
    assert before["last_message_at"] == 0
    assert not any(item.get("type") == "message" for item in _items(page, base_url, session_id))

    row = _leave_chat(page, base_url, session_id)
    title = f"empty-metadata-{uuid.uuid4().hex[:8]}"
    response = page.request.patch(
        f"{base_url}/v1/sessions/{session_id}",
        data={"title": title},
    )
    assert response.ok, response.text()
    expect(row).to_contain_text(title, timeout=20_000)
    expect(_unread_dot(row)).to_have_count(0)

    after = _list_item(page, base_url, session_id)
    assert after["last_message_at"] == 0


def test_archive_unarchive_keeps_read_watermark_live(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Archiving and restoring a read session does not create a false dot."""
    base_url, session_id = seeded_session
    _seed_visible_message(page, base_url, session_id, "Archive lifecycle baseline.")

    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_unread_dot(row)).to_have_count(0)
    before_last_message_at = _list_item(page, base_url, session_id)["last_message_at"]
    row = _leave_chat(page, base_url, session_id)

    archive_response = page.request.patch(
        f"{base_url}/v1/sessions/{session_id}",
        data={"archived": True},
    )
    assert archive_response.ok, archive_response.text()
    expect(row).to_have_count(0, timeout=20_000)

    unarchive_response = page.request.patch(
        f"{base_url}/v1/sessions/{session_id}",
        data={"archived": False},
    )
    assert unarchive_response.ok, unarchive_response.text()
    # The archived row leaves the updates watch-set, so an external unarchive
    # is rediscovered by the normal 45–60 s fallback list refresh.
    expect(row).to_be_visible(timeout=90_000)
    expect(_unread_dot(row)).to_have_count(0)
    assert _list_item(page, base_url, session_id)["last_message_at"] == before_last_message_at


def test_visible_message_reaches_unread_via_real_fallback_poll(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A disconnected updates stream falls back to the real session list poll."""
    base_url, session_id = seeded_session
    _seed_visible_message(page, base_url, session_id, "Fallback baseline.")
    list_requests: list[str] = []

    def record_list_request(request: Any) -> None:
        if request.method == "GET" and "/v1/sessions?" in request.url:
            list_requests.append(request.url)

    # This blocks only the updates WebSocket. REST responses remain real, so
    # the assertion below proves the documented fallback rather than a fixture.
    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.on("request", record_list_request)
    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_unread_dot(row)).to_have_count(0)
    _leave_chat(page, base_url, session_id)
    before_last_message_at = _list_item(page, base_url, session_id)["last_message_at"]
    initial_list_requests = len(list_requests)

    live_text = f"fallback-away-{uuid.uuid4().hex[:8]}"
    _append_assistant_message(page, base_url, session_id, live_text)

    # The normal mine-session fallback is 60 s; keep this explicit upper bound
    # long enough to distinguish a dead poll from a slow but real response.
    expect(_unread_dot(row)).to_be_visible(timeout=90_000)
    assert len(list_requests) > initial_list_requests, (
        "the unread dot appeared without a WebSocket, but no fallback "
        f"GET /v1/sessions request was observed: {list_requests}"
    )
    assert _list_item(page, base_url, session_id)["last_message_at"] > before_last_message_at
    assert any(
        live_text in str(item.get("content")) for item in _items(page, base_url, session_id)
    )

    row.locator(f'a[href="/c/{session_id}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_id}")
    expect(_unread_dot(_row(page, session_id))).to_have_count(0)
