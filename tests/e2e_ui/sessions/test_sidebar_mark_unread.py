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
a real reload, survives it specifically via ``localStorage`` when the serving
replica's seed is empty, and clears after the user deliberately reopens the
thread following navigation away.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry
from tests.e2e_ui.sessions.unread_helpers import append_assistant_message


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


@pytest.mark.parametrize("mark_entrypoint", ["row-kebab", "context-menu"])
def test_marked_unread_clears_when_reopened_after_inbox_navigation(
    page: Page,
    seeded_session: tuple[str, str],
    mark_entrypoint: str,
) -> None:
    """Reopening a flagged thread after leaving it counts as reading it.

    Marking the active session unread is intentionally durable across a hard
    reload. A deliberate navigation away and back is different: returning to
    the thread should clear the explicit unread override and its sidebar dot.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_unread_dot(row)).to_have_count(0)

    if mark_entrypoint == "row-kebab":
        row.hover()
        row.get_by_test_id("conversation-actions").click()
    else:
        row.locator(f'a[href="/c/{session_id}"]').click(button="right")
    page.get_by_test_id("mark-unread-conversation").click()
    expect(_unread_dot(row)).to_be_visible()

    # Inbox is a separate route, so ChatPage unmounts. Returning through the
    # persistent sidebar exercises the real reopen path rather than a simple
    # in-place /c/a → /c/b switch.
    page.get_by_test_id("inbox-button").click()
    expect(page).to_have_url(f"{base_url}/inbox")
    expect(page.get_by_role("heading", name="Inbox")).to_be_visible()

    inbox_row = _row(page, session_id)
    expect(inbox_row).to_be_visible()
    inbox_row.locator(f'a[href="/c/{session_id}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_id}")

    expect(_unread_dot(_row(page, session_id))).to_have_count(0)


def test_metadata_update_does_not_light_dot_without_new_items(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A title-only update must not look like an unread assistant turn."""
    base_url, session_id = seeded_session
    append_assistant_message(
        page,
        base_url,
        session_id,
        "Existing assistant answer for metadata coverage.",
    )

    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_unread_dot(row)).to_have_count(0)

    before_items_response = page.request.get(
        f"{base_url}/v1/sessions/{session_id}/items?limit=100&order=asc"
    )
    assert before_items_response.ok, before_items_response.text()
    before_items = before_items_response.json()["data"]
    before_content_items = [item for item in before_items if item.get("type") == "message"]
    assert before_content_items, "the metadata test needs an existing visible transcript"

    before_session_response = page.request.get(f"{base_url}/v1/sessions/{session_id}")
    assert before_session_response.ok, before_session_response.text()
    before_updated_at = int(before_session_response.json()["updated_at"])

    # Leave the chat so the active-view read watermark is established, then
    # allow the read watermark to age past the server's whole-second clock
    # before the metadata write. A same-second PATCH would not distinguish
    # this regression from an unchanged session.
    page.get_by_test_id("inbox-button").click()
    expect(page).to_have_url(f"{base_url}/inbox")
    page.wait_for_timeout(2_100)

    title = f"e2e-metadata-{int(time.time() * 1000)}"
    update_response = page.request.patch(
        f"{base_url}/v1/sessions/{session_id}",
        data={"title": title},
    )
    assert update_response.ok, update_response.text()
    updated_session = update_response.json()
    assert int(updated_session["updated_at"]) > before_updated_at

    after_items_response = page.request.get(
        f"{base_url}/v1/sessions/{session_id}/items?limit=100&order=asc"
    )
    assert after_items_response.ok, after_items_response.text()
    after_content_items = [
        item for item in after_items_response.json()["data"] if item.get("type") == "message"
    ]
    assert after_content_items == before_content_items

    # Reload the list so the assertion observes the metadata write rather than
    # racing the sidebar's refresh/updates stream.
    page.reload()
    inbox_row = _row(page, session_id)
    expect(inbox_row).to_be_visible()
    expect(inbox_row).to_contain_text(title)
    expect(_unread_dot(inbox_row)).to_have_count(0)


def test_new_visible_message_lights_dot_and_reopen_clears_it(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A later visible turn lights the dot, and opening it marks it read."""
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_unread_dot(row)).to_have_count(0)

    page.get_by_test_id("inbox-button").click()
    expect(page).to_have_url(f"{base_url}/inbox")
    # Establish the active-chat watermark before the later turn is appended.
    page.wait_for_timeout(2_100)
    append_assistant_message(
        page,
        base_url,
        session_id,
        "A later visible assistant answer.",
    )

    page.reload()
    inbox_row = _row(page, session_id)
    expect(inbox_row).to_be_visible()
    expect(_unread_dot(inbox_row)).to_be_visible()

    inbox_row.locator(f'a[href="/c/{session_id}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_id}")
    expect(_unread_dot(_row(page, session_id))).to_have_count(0)


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
