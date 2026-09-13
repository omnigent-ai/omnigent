"""Mobile composer Enter inserts a newline; only the Send button submits."""

from __future__ import annotations

import json
import os
from typing import Any

from playwright.sync_api import Browser, Route, expect

_MOBILE_VIEWPORT = {"width": 390, "height": 844}
_PROMPT = "first line\nsecond line"


def test_mobile_enter_inserts_newline_and_send_submits_once(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    context = browser.new_context(
        viewport=_MOBILE_VIEWPORT,
        has_touch=True,
        is_mobile=True,
        record_video_dir=os.environ.get("OMNIGENT_E2E_RECORD_DIR"),
    )
    page = context.new_page()
    event_posts: list[dict[str, Any]] = []

    def capture_event(route: Route) -> None:
        request = route.request
        if request.method == "POST":
            event_posts.append(request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_mobile_enter"}),
        )

    page.route(f"**/v1/sessions/{session_id}/events", capture_event)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        assert page.evaluate("matchMedia('(max-width: 767.98px)').matches")

        composer.fill("first line")
        composer.press("Enter")
        composer.type("second line")

        expect(composer).to_have_value(_PROMPT)
        assert event_posts == []

        send = page.get_by_role("button", name="Send", exact=True)
        expect(send).to_be_visible()
        send.click()

        expect(
            page.locator('[data-testid="message-bubble"][data-role="user"]', has_text="first line")
        ).to_have_count(1)
        assert len(event_posts) == 1
        content = event_posts[0]["data"]["content"]
        assert content[0]["text"] == _PROMPT
    finally:
        context.close()
