"""Browser e2e for the sidebar's session search box.

The search input (``aria-label="Search sessions"``) debounces keystrokes
(~300 ms) and forwards the query to the server as
``GET /v1/sessions?search_query=…`` — filtering is server-side, a
case-insensitive substring match on the session title or conversation
content (see ``list_sessions`` in ``routes/sessions.py``). A matching
query keeps the row; a non-matching one drops it and the list falls to
its "No matching conversations" empty state.

This drives the full chain the ``useConversations`` unit test can't: the
debounce → the ``?search_query=`` round trip → the re-rendered list. A
regression in the query param wiring, the debounce, or the empty-state
copy would surface here.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect


def test_search_filters_sessions(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Typing a query narrows the list to matching sessions and back.

    Sets a unique title on the seeded session, then asserts the
    round-trip both ways:

    - A query matching the title keeps the row visible.
    - A query that matches nothing drops the row and surfaces the
      "No matching conversations" empty state.

    The unique marker (a uuid) can't collide with other tests' sessions
    in the shared server, so the non-matching assertion is deterministic.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    marker = uuid.uuid4().hex[:12]
    title = f"e2e-search-{marker}"
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()

    page.goto(f"{base_url}/c/{session_id}")

    row = page.locator(f'a[href="/c/{session_id}"]')
    search = page.get_by_role("searchbox", name="Search sessions")
    # Baseline: with no query the row is listed.
    expect(row).to_be_visible()

    # A query that matches nothing empties the list (debounce + server
    # round trip resolve within the default expect timeout).
    no_match = f"zzz-no-match-{uuid.uuid4().hex[:12]}"
    search.fill(no_match)
    expect(row).to_have_count(0)
    expect(page.get_by_text("No matching conversations")).to_be_visible()

    # A query matching the title brings the row back — proving the box
    # filters server-side on the title, not just hides everything.
    search.fill(marker)
    expect(row).to_be_visible()
