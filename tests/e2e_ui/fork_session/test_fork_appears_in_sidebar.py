"""Browser e2e: a forked session stays listed in the sidebar while the session
list lags and no ``session_added`` push arrives, like a created session.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest
from playwright.sync_api import Page, Route, WebSocketRoute, expect

from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry

_MARKER = "rambutan-no-push-marker"
_ROW_TIMEOUT_MS = 20_000
_STALE_LIST_S = 30.0

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# List GETs only: ``/v1/sessions?…`` but not ``/v1/sessions/{id}`` or the fork POST.
_LIST_RE = re.compile(r"/v1/sessions\?[^/]*$")


def _row(page: Page, session_id: str):
    return page.locator(f'a[href="/c/{session_id}"]')


def _fork_from_first_response(page: Page, source_id: str) -> str:
    """Click the first assistant bubble's Fork action, submit Clone, return the fork id."""
    first_assistant = page.locator(_ASSISTANT).first
    expect(first_assistant).to_be_visible(timeout=30_000)
    first_assistant.hover()
    first_assistant.get_by_test_id("fork-from-response").click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_have_text("Clone")
    submit.click()
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(source_id)})(conv_)?[0-9a-f]+"),
        timeout=30_000,
    )
    expect(dialog).not_to_be_visible()
    return page.url.rsplit("/c/", 1)[1].split("?", 1)[0]


def _lagging_list_routes(page: Page) -> tuple[set[str], list[float], list[dict[str, Any]]]:
    """Hide every new fork from list reads for ``_STALE_LIST_S`` after its fork request."""
    fork_ids: set[str] = set()
    stale_until = [0.0]
    stale_served: list[dict[str, Any]] = []

    def record_fork(route: Route) -> None:
        response = fetch_with_retry(route)
        if response.ok:
            fork_ids.add(response.json()["id"])
            stale_until[0] = time.monotonic() + _STALE_LIST_S
        route.fulfill(response=response)

    def lag_list(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        if not response.ok or time.monotonic() >= stale_until[0]:
            route.fulfill(response=response)
            return
        body: dict[str, Any] = response.json()
        original_ids = [row["id"] for row in body["data"]]
        body["data"] = [row for row in body["data"] if row["id"] not in fork_ids]
        removed = [i for i in original_ids if i in fork_ids]
        stale_served.append({"url": route.request.url, "removed_fork_ids": removed})
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route(re.compile(r"/v1/sessions/[^/]+/fork$"), record_fork)
    page.route(_LIST_RE, lag_list)
    return fork_ids, stale_until, stale_served


def _seed_turn_from_composer(page: Page, base_url: str, source_id: str, marker: str) -> None:
    """Send one marked message so the chat shows a committed exchange to fork from."""
    page.goto(f"{base_url}/c/{source_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    composer.fill(f"Reply with one short word. Marker: {marker}")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(_row(page, source_id)).to_be_visible()


def test_fork_row_while_list_lags_without_live_updates(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Lagging list and no live-updates stream: the fork row appears at once and stays."""
    base_url, source_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url, [{"text": "OK"}], key="fork-no-push-seed", match=_MARKER
    )

    page: Page = request.getfixturevalue("page")
    silenced_sockets: list[str] = []

    def silence_updates_stream(ws: WebSocketRoute) -> None:
        # Intercept but never connect to the server: the socket stays open yet
        # delivers no ``session_added`` push, modelling a dropped live update.
        silenced_sockets.append(ws.url)

    page.route_web_socket(re.compile(r"/v1/sessions/updates"), silence_updates_stream)
    fork_ids, stale_until, stale_served = _lagging_list_routes(page)
    _seed_turn_from_composer(page, base_url, source_id, _MARKER)
    assert silenced_sockets, "the SPA never opened the session-updates socket"

    fork_id = _fork_from_first_response(page, source_id)
    assert fork_id in fork_ids, (fork_id, fork_ids)
    row = _row(page, fork_id)
    # The dialog paints the row itself: no push and no caught-up list needed.
    expect(row).to_be_visible(timeout=_ROW_TIMEOUT_MS)

    # Every list refetch inside the lag window omits the fork; the keep-alive
    # must stop those responses from evicting the row.
    gaps: list[float] = []
    started = time.monotonic()
    while time.monotonic() < stale_until[0]:
        page.wait_for_timeout(1_000)
        if row.count() == 0 or not row.first.is_visible():
            gaps.append(round(time.monotonic() - started, 1))
    assert stale_served, "no list response was served without the fork during the stale window"
    assert gaps == [], f"fork row disappeared at {gaps}s while the list lagged"

    # Server-side the fork is real: a fresh load once the lag clears lists it too.
    page.reload()
    expect(row).to_be_visible(timeout=_ROW_TIMEOUT_MS)
