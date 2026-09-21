"""E2E: a transient session-load failure must not block sending or lose the message.

The reported journey: the browser's ``GET /v1/sessions/{id}`` failed
with a 500 while the server was overloaded. The web client only sent the first
message after that snapshot had loaded, so the message was never posted, the
page replaced itself with "Conversation not found", and the typed text was gone.

What this drives, against a real spawned server and the built SPA:

* With the snapshot GET answering 500, the chat surface still renders (no
  "Conversation not found"), the composer works, and a send reaches
  ``POST /v1/sessions/{id}/events`` — history is not a prerequisite for sending.
* Once the bounded retries are exhausted the page shows a neutral history
  notice; after the server recovers, its Retry button clears it in place.
* A send whose POST fails keeps a durable copy: after a reload the text is back
  in the composer and a resend carries the same ``stable_id``, so the server
  can dedupe it.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, Route, expect

_NOTICE_TEXT = "Conversation history is temporarily unavailable."
_INTERNAL_ERROR = json.dumps(
    {"error": {"code": "internal_error", "message": "An internal error occurred."}}
)


def _snapshot_pattern(session_id: str) -> re.Pattern[str]:
    # The session snapshot GET, with or without its query string — never the
    # ``/items``, ``/stream`` or ``/events`` routes beneath it.
    return re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?.*)?$")


def _composer(page: Page):
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    return composer


def _send(page: Page, text: str) -> None:
    composer = _composer(page)
    expect(composer).to_be_enabled(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wait_for(page: Page, predicate: Callable[[], bool], *, timeout_s: float = 15.0) -> None:
    """Poll *predicate* (a Python-side condition) until true, yielding to the page."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met within timeout")
        page.wait_for_timeout(100)


def test_send_works_and_history_notice_retries_when_snapshot_fails(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    failing = {"snapshot": True}

    def fail_snapshot(route: Route) -> None:
        if route.request.method == "GET" and failing["snapshot"]:
            route.fulfill(status=500, content_type="application/json", body=_INTERNAL_ERROR)
        else:
            route.continue_()

    posted: list[dict[str, Any]] = []

    def record_post(route: Route) -> None:
        if route.request.method == "POST":
            posted.append(json.loads(route.request.post_data or "{}"))
        route.continue_()

    page.route(_snapshot_pattern(session_id), fail_snapshot)
    page.route(f"**/v1/sessions/{session_id}/events", record_post)
    page.goto(f"{base_url}/c/{session_id}")

    # The chat renders even though its history could not be loaded.
    composer = _composer(page)
    expect(page.get_by_text("Conversation not found")).to_have_count(0)
    notice = page.get_by_role("status").filter(has_text=_NOTICE_TEXT)
    expect(notice).to_be_visible(timeout=15_000)

    # Sending does not wait for history: the message is posted and shown.
    _send(page, "delivered despite the snapshot failure")
    expect(
        page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
            has_text="delivered despite the snapshot failure"
        )
    ).to_be_visible(timeout=15_000)
    _wait_for(page, lambda: [p.get("type") for p in posted] == ["message"])
    expect(composer).to_have_value("")

    # Sending leaves the live stream and its history state alone: no rebind, no
    # silent retry, so the notice is still up until the user asks for one.
    expect(notice).to_be_visible(timeout=20_000)
    page.screenshot(path=tmp_path / "history-unavailable-notice.png")

    # The server recovers; Retry reloads history in place and the notice goes.
    failing["snapshot"] = False
    page.get_by_role("button", name="Retry", exact=True).click()
    expect(notice).to_have_count(0, timeout=15_000)
    expect(page.get_by_text("Conversation not found")).to_have_count(0)


def test_failed_send_is_recovered_after_reload_with_the_same_identity(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    failing = {"post": True}
    stable_ids: list[str] = []

    def fail_post(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        body = json.loads(route.request.post_data or "{}")
        stable_ids.append(body.get("data", {}).get("stable_id", ""))
        if failing["post"]:
            route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps(
                    {"error": {"code": "runner_unavailable", "message": "No runner is available."}}
                ),
            )
        else:
            route.continue_()

    page.route(f"**/v1/sessions/{session_id}/events", fail_post)
    page.goto(f"{base_url}/c/{session_id}")
    _send(page, "keep me across a reload")
    # The failed send hands its text back to the composer in this page.
    expect(_composer(page)).to_have_value("keep me across a reload", timeout=15_000)
    assert len(stable_ids) == 1 and stable_ids[0]

    # A reload keeps the text, and a resend carries the same send identity.
    page.reload()
    expect(_composer(page)).to_have_value("keep me across a reload", timeout=30_000)
    page.screenshot(path=tmp_path / "recovered-after-reload.png")
    failing["post"] = False
    page.get_by_role("button", name="Send", exact=True).click()
    _wait_for(page, lambda: len(stable_ids) == 2)
    assert stable_ids[1] == stable_ids[0]
