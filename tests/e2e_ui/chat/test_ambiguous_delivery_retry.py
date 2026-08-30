"""E2E: an accepted background message survives an ambiguous browser retry."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any

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


def _send(page: Page, text: str) -> None:
    page.get_by_label(_COMPOSER_LABEL).fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wait_for_gate(page: Page, mock_url: str) -> None:
    _wait_for(
        page,
        lambda: bool(httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json()["pending"]),
    )


def _release_gate(mock_url: str) -> None:
    response = httpx.post(f"{mock_url}/gate/release", timeout=5.0)
    response.raise_for_status()
    assert response.json()["released"] is True


def _runner_requests_containing(mock_url: str, text: str) -> list[dict[str, Any]]:
    response = httpx.get(f"{mock_url}/mock/requests", timeout=5.0)
    response.raise_for_status()
    return [
        request
        for request in response.json()["requests"]
        if isinstance(request, dict) and text in json.dumps(request)
    ]


def test_accepted_background_message_is_dispatched_once_after_response_loss(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
    mock_llm_server_url: str,
) -> None:
    """One queued submit may retry its POST, but must not run a second turn."""
    base_url, session_id, other_session_id = seeded_session_pair
    mock_url = mock_llm_server_url
    setup_text = f"hold-open-{uuid.uuid4().hex[:8]}"
    queued_text = f"ambiguous-delivery-{uuid.uuid4().hex[:8]}"
    accepted_attempts: list[dict[str, Any]] = []
    configure_mock_llm(
        mock_url,
        [{"block": True, "text": "setup turn released"}],
        key=f"ambiguous-delivery-{uuid.uuid4().hex[:8]}",
        match=setup_text,
    )

    def hide_first_accepted_response(route: Route, request: Request) -> None:
        body = request.post_data_json
        if (
            request.method != "POST"
            or not isinstance(body, dict)
            or body.get("type") != "message"
            or queued_text not in json.dumps(body)
        ):
            route.continue_()
            return

        upstream = route.fetch()
        accepted_attempts.append(
            {
                "request": body,
                "status": upstream.status,
                "response": upstream.json(),
            }
        )
        if len(accepted_attempts) == 1:
            # The server has already persisted and dispatched this request.
            # Hide that accepted response from fetch so the real queue code
            # treats the delivery result as ambiguous and retries later.
            route.abort("failed")
        else:
            route.fulfill(response=upstream)

    page.route(f"**/v1/sessions/{session_id}/events", hide_first_accepted_response)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)

    _send(page, setup_text)
    _wait_for_gate(page, mock_url)

    # This is the only submit of the logical message under test. The busy
    # session puts it into the product's real client-side queue.
    _send(page, queued_text)
    expect(page.get_by_test_id("composer-queued-strip")).to_contain_text(queued_text)

    # Switch conversations before the active turn finishes so
    # QueueFlushProvider, not the viewed conversation's composer effect, owns
    # delivery. Clicking the SPA sidebar link preserves the in-memory queue.
    page.locator(f'a[href="/c/{other_session_id}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{other_session_id}")
    _release_gate(mock_url)

    _wait_for(page, lambda: len(accepted_attempts) >= 2, timeout_s=90.0)

    response_item_ids = [
        attempt["response"].get("item_id")
        for attempt in accepted_attempts
        if isinstance(attempt["response"], dict)
    ]
    if len(set(response_item_ids)) > 1:
        _wait_for(
            page,
            lambda: len(_runner_requests_containing(mock_url, queued_text)) >= 2,
            timeout_s=60.0,
        )
    runner_requests = _runner_requests_containing(mock_url, queued_text)

    evidence = {
        "browser_post_attempts": len(accepted_attempts),
        "request_client_event_ids": [
            attempt["request"].get("client_event_id") for attempt in accepted_attempts
        ],
        "accepted_statuses": [attempt["status"] for attempt in accepted_attempts],
        "accepted_item_ids": response_item_ids,
        "runner_dispatches": len(runner_requests),
    }
    problems: list[str] = []
    if len(accepted_attempts) != 2:
        problems.append("the one queued entry did not make exactly two browser POST attempts")
    client_event_ids = evidence["request_client_event_ids"]
    if not client_event_ids[0] or len(set(client_event_ids)) != 1:
        problems.append("ambiguous retries did not preserve one client_event_id")
    if len(set(response_item_ids)) != 1:
        problems.append("the server created more than one durable receipt")
    if len(runner_requests) != 1:
        problems.append("the runner received more than one logical dispatch")

    assert not problems, f"{'; '.join(problems)}\n{json.dumps(evidence, indent=2)}"
