"""E2E: a queued user message whose accepted POST loses its response must not duplicate.

The browser POSTs a
queued user message, the server accepts it (202 — the message is persisted
and a turn is dispatched), but the response never reaches the browser
(flaky network / proxy drop). The client cannot tell the send succeeded,
treats it as failed, and re-sends the same message — so the server persists
a second identical user message and runs a second turn. The user sees
their message twice in the transcript.

The test drives the real SPA journey:

    send a message while the agent is busy -> it parks in the queue strip ->
    switch to another session -> the held turn finishes and the queued POST
    fires; its response is lost in flight (injected fault) -> switch back ->
    the message must appear exactly once.

On the buggy build the message is persisted twice (once per POST attempt)
and renders as two identical user bubbles, so the final assertions fail.
The fault injection (aborting the response of the first *accepted* POST)
models the ambiguous-delivery trigger: the server committed the message,
the browser cannot know that.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, Request, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER_LABEL = "Message the agent"


def _wait_for(page: Page, predicate: Callable[[], bool], *, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _session_is_idle(base_url: str, session_id: str) -> bool:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=5.0)
    response.raise_for_status()
    return response.json()["status"] == "idle"


def _gate_is_pending(mock_url: str) -> bool:
    response = httpx.get(f"{mock_url}/gate/pending", timeout=5.0)
    response.raise_for_status()
    return bool(response.json()["pending"])


def _release_gate(mock_url: str) -> None:
    response = httpx.post(f"{mock_url}/gate/release", timeout=5.0)
    response.raise_for_status()
    assert response.json()["released"] is True


def _matching_user_items(base_url: str, session_id: str, text: str) -> list[dict[str, Any]]:
    """Committed user-message items whose text is exactly *text*."""
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=15.0,
    )
    response.raise_for_status()
    matches: list[dict[str, Any]] = []
    for item in response.json().get("data", []):
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        item_text = " ".join(
            str(block["text"])
            for block in item.get("content", [])
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
        if item_text == text:
            matches.append(item)
    return matches


def _message_text(request: Request, session_id: str) -> str | None:
    """The input_text of a user-message POST to *session_id*, else None."""
    if request.method != "POST":
        return None
    if urlparse(request.url).path != f"/v1/sessions/{session_id}/events":
        return None
    body = request.post_data_json
    if not isinstance(body, dict) or body.get("type") != "message":
        return None
    for block in body.get("data", {}).get("content", []):
        if isinstance(block, dict) and block.get("type") == "input_text":
            return str(block.get("text", ""))
    return None


def test_lost_post_response_does_not_duplicate_user_message(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
    mock_llm_server_url: str,
) -> None:
    """One queued submit whose accepted POST response is lost persists once."""
    base_url, session_id, other_session_id = seeded_session_pair
    mock_url = mock_llm_server_url
    run_tag = uuid.uuid4().hex[:8]
    setup_text = f"hold-the-turn-open-{run_tag}"
    sentinel = f"queued-once-{run_tag} deliver exactly once"

    # The setup turn parks on the mock LLM's gate so the session stays busy
    # for as long as the test needs (content-routed by the setup text).
    configure_mock_llm(
        mock_url,
        [{"block": True, "text": "setup turn released"}],
        key=f"queued-once-{run_tag}",
        match=setup_text,
    )

    post_attempt_statuses: list[int] = []

    def hide_first_accepted_response(route: Route, request: Request) -> None:
        if _message_text(request, session_id) != sentinel:
            route.continue_()
            return
        upstream = route.fetch()
        post_attempt_statuses.append(upstream.status)
        if len(post_attempt_statuses) == 1:
            # The server has already accepted and persisted this message.
            # Hide that response so the client's delivery result is
            # ambiguous — the real-world lost-response fault.
            route.abort("failed")
        else:
            route.fulfill(response=upstream)

    page.route(f"**/v1/sessions/{session_id}/events", hide_first_accepted_response)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)

    # 1. Occupy the agent: this turn blocks on the mock LLM's gate.
    composer.fill(setup_text)
    composer.press("Enter")
    _wait_for(page, lambda: _gate_is_pending(mock_url), timeout_s=60.0)

    # 2. Send the message under test while the agent is busy — it queues.
    composer.fill(sentinel)
    composer.press("Enter")
    expect(page.get_by_test_id("composer-queued-strip")).to_contain_text(sentinel)

    # 3. Switch to the other session; the queue drains in the background.
    page.locator(f'a[href="/c/{other_session_id}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{other_session_id}", timeout=30_000)

    # 4. Let the held turn finish. The background flush POSTs the queued
    #    message; the injected fault swallows the accepted response.
    _release_gate(mock_url)
    _wait_for(page, lambda: len(post_attempt_statuses) >= 1, timeout_s=90.0)
    _wait_for(page, lambda: _session_is_idle(base_url, session_id), timeout_s=60.0)

    # 5. Wait past the client's retry cooldown, then return to the
    #    conversation — the journey step that lets the client re-send.
    page.wait_for_timeout(6_000)
    source_link = page.locator(f'a[href="/c/{session_id}"]')
    expect(source_link).to_be_visible(timeout=30_000)
    source_link.click()
    expect(page).to_have_url(f"{base_url}/c/{session_id}", timeout=30_000)

    # Give a buggy build ample time to fire its duplicate POST and persist
    # it; a fixed build simply idles through this window (the wait times out
    # without ever seeing a second copy — the correct behaviour).
    with contextlib.suppress(AssertionError):
        _wait_for(
            page,
            lambda: len(_matching_user_items(base_url, session_id, sentinel)) >= 2,
            timeout_s=15.0,
        )
    page.wait_for_timeout(2_000)

    stored = _matching_user_items(base_url, session_id, sentinel)
    bubbles = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=sentinel
    )
    evidence = {
        "post_attempt_statuses": post_attempt_statuses,
        "stored_copies": len(stored),
        "rendered_user_bubbles": bubbles.count(),
    }
    assert len(stored) == 1, (
        "duplicate user message: one queued submit was persisted "
        f"{len(stored)} times\n{json.dumps(evidence, indent=2)}"
    )
    expect(bubbles).to_have_count(1)
