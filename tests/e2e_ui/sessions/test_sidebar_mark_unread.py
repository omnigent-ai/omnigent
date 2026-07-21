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
the server seed only ever *raises* a baseline (max-merge). These tests
guard the wiring the mocked unit tests can't — that the real dot survives
a real reload, and survives it specifically via ``localStorage`` when the
serving replica's seed is empty.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page, Route, expect


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _unread_dot(row: Locator) -> Locator:
    """Locate the row's unread (pink) dot — the unseen session-state badge."""
    return row.locator('[data-testid="session-state-badge"][data-state="unseen"]')


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
        response = route.fetch()
        payload = response.json()
        for conv in payload.get("data", []):
            conv["viewer_unread"] = False
            conv["viewer_last_seen"] = None
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
