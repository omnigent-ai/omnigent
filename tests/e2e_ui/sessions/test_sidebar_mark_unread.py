"""Browser e2e for the sidebar unread dot's read-state persistence.

The row kebab's "Mark as unread" item re-lights the row's unread dot
(``SessionStateBadge`` with ``data-state="unseen"``) and writes the
caller's read-state via ``PUT /v1/sessions/{id}/read-state``.

Read-state is **browser-durable**: the baseline lives in ``localStorage``
and is mirrored best-effort to the server, whose copy is in-memory and
per-replica. Under replica sharding a reload's ``GET /v1/sessions`` can
land on a pod that never saw the user's PUT, so its ``viewer_unread`` /
``viewer_last_seen`` fields can be absent even for a session the user
just acted on. The client's ``localStorage`` copy is the durable source;
a server value replaces it only when its ``viewer_read_state_revision`` is
newer than the revision the client last applied. These tests
guard the wiring the mocked unit tests can't — that the real dot survives
a real reload, and survives it specifically via ``localStorage`` when the
serving replica's seed is empty. The cross-client tests pin that a read or
mark-unread on one client reaches the same user's other open client.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Browser, Locator, Page, Response, Route, expect

from tests._helpers.browser import user_page
from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry

# Spans one connected-stream list refetch (60 s), so push or poll delivery both pass.
_OTHER_CLIENT_TIMEOUT_MS = 70_000


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _unread_dot(row: Locator) -> Locator:
    """Locate the row's unread (pink) dot — the unseen session-state badge."""
    return row.locator('[data-testid="session-state-badge"][data-state="unseen"]')


def _is_read_state_put(session_id: str) -> Callable[[Response], bool]:
    return lambda response: (
        response.request.method == "PUT"
        and response.url.endswith(f"/v1/sessions/{session_id}/read-state")
    )


def test_mark_unread_lights_the_dot_and_persists_across_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Marking a session unread lights the dot and survives a reload.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound (idle) session.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()
    # The row starts seen — no unread dot.
    expect(_unread_dot(row)).to_have_count(0)

    # Open the row kebab and pick "Mark as unread". Hover first so the
    # desktop hover-revealed kebab trigger is interactable.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("mark-unread-conversation").click()

    # The dot lights immediately (optimistic mirror write), even though this
    # is the session you're currently viewing.
    expect(_unread_dot(row)).to_be_visible()

    # Reload: the dot must come back. The mark-unread persisted the baseline
    # to localStorage AND best-effort PUT it to the server, so either source
    # re-lights it after a fresh page load.
    page.reload()
    expect(_row(page, session_id)).to_be_visible()
    expect(_unread_dot(_row(page, session_id))).to_be_visible()


def test_unread_dot_survives_reload_from_localStorage_when_server_seed_is_empty(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The dot survives a reload via localStorage even when the serving
    replica's read-state seed is empty.

    This is the pod-independence contract: under replica sharding the
    reload's ``GET /v1/sessions`` may hit a pod whose in-memory read-state
    never saw the mark-unread PUT, so it returns ``viewer_unread=false`` /
    ``viewer_last_seen=null``. The dot must still light — proving the
    baseline was restored from ``localStorage``, not the server seed.

    Pre-``localStorage`` this row would read as *seen* after such a seed
    (no client baseline + a read-state-less server row), so the dot would
    be gone; asserting it is present pins the new durable behavior.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound (idle) session.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_unread_dot(row)).to_have_count(0)

    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("mark-unread-conversation").click()
    expect(_unread_dot(row)).to_be_visible()

    # Simulate the reload landing on a replica whose seed lacks this user's
    # read-state: strip viewer_unread / viewer_last_seen from every row of
    # the list response the reloaded page fetches. The list is
    # ``GET /v1/sessions`` → ``{ data: [conv, ...], ... }`` (ConversationsPage).
    def _strip_read_state(route: Route) -> None:
        request = route.request
        parsed = urlparse(request.url)
        # Only the list endpoint (not /v1/sessions/{id} or sub-resources).
        if request.method != "GET" or parsed.path != "/v1/sessions":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        for conv in payload.get("data", []):
            conv["viewer_unread"] = False
            conv["viewer_last_seen"] = None
            conv["viewer_read_state_revision"] = None
        route.fulfill(
            status=response.status,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions?*", _strip_read_state)

    # Reload: the server seed now carries no read-state for this session, so
    # the dot can ONLY come from localStorage. Its presence proves the
    # browser-durable baseline survived and is pod-independent.
    page.reload()
    reloaded = _row(page, session_id)
    expect(reloaded).to_be_visible()
    expect(_unread_dot(reloaded)).to_be_visible()


def test_reading_on_one_client_clears_the_dot_on_another_open_client(
    page: Page,
    browser: Browser,
    browser_context_args: dict[str, Any],
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Opening a finished session on one client clears its dot on the user's other open client."""
    base_url, session_id = seeded_session
    marker = f"cross-client-{uuid.uuid4().hex[:8]}"
    # Extra copies so a title request carrying the marker cannot drain the reply.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Task finished: all tests green.", "delay": 4.0}] * 3,
        match=marker,
    )

    with user_page(browser, headers={}, **browser_context_args) as other:
        page.goto(f"{base_url}/inbox")
        expect(_row(page, session_id)).to_be_visible()

        other.goto(f"{base_url}/c/{session_id}")
        composer = other.get_by_label("Message the agent")
        composer.fill(f"Finish the task and report back. Marker: {marker}")
        composer.press("Enter")
        expect(
            other.locator('[data-testid="message-bubble"][data-role="user"]').filter(
                has_text=marker
            )
        ).to_be_visible()
        other.locator('a[href="/inbox"]').first.click()

        expect(_unread_dot(_row(page, session_id))).to_be_visible(timeout=60_000)
        expect(_unread_dot(_row(other, session_id))).to_be_visible(timeout=60_000)

        with page.expect_response(_is_read_state_put(session_id)) as put:
            _row(page, session_id).locator(f'a[href="/c/{session_id}"]').click()
        assert put.value.ok
        expect(_unread_dot(_row(page, session_id))).to_have_count(0)

        expect(_unread_dot(_row(other, session_id))).to_have_count(
            0, timeout=_OTHER_CLIENT_TIMEOUT_MS
        )


@pytest.mark.parametrize("reload_other_client", [False, True], ids=["live", "after-reload"])
def test_marking_unread_on_one_client_lights_the_dot_on_another_open_client(
    page: Page,
    browser: Browser,
    browser_context_args: dict[str, Any],
    seeded_session: tuple[str, str],
    reload_other_client: bool,
) -> None:
    """Mark as unread on one client lights the dot on the user's other client, live or reloaded."""
    base_url, session_id = seeded_session

    with user_page(browser, headers={}, **browser_context_args) as other:
        other.goto(f"{base_url}/inbox")
        expect(_row(other, session_id)).to_be_visible()
        expect(_unread_dot(_row(other, session_id))).to_have_count(0)

        page.goto(f"{base_url}/inbox")
        row = _row(page, session_id)
        row.hover()
        row.get_by_test_id("conversation-actions").click()
        with page.expect_response(_is_read_state_put(session_id)) as put:
            page.get_by_test_id("mark-unread-conversation").click()
        assert put.value.ok
        expect(_unread_dot(row)).to_be_visible()

        if reload_other_client:
            other.reload()
        expect(_unread_dot(_row(other, session_id))).to_be_visible(
            timeout=10_000 if reload_other_client else _OTHER_CLIENT_TIMEOUT_MS
        )
