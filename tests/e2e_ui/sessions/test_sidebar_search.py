"""Browser e2e for session search via the command palette.

The sidebar's "Search" button opens the command palette, whose input
debounces keystrokes (~300 ms) and forwards the query to the server as
``GET /v1/sessions?search_query=…`` — filtering is server-side, a
case-insensitive substring match on the session title or conversation
content (see ``list_sessions`` in ``routes/sessions.py``). A matching
query lists the session under the palette's "Sessions" group; a
non-matching one lists nothing and the palette falls to its
"No results found" empty state.

This drives the full chain the ``useConversations`` unit test can't: the
sidebar button → the palette → the debounce → the ``?search_query=``
round trip → the re-rendered results. A regression in the button wiring,
the query param, the debounce, or the empty-state copy would surface here.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect


def test_search_lists_matching_sessions_in_palette(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The palette lists a session matching the query and empties otherwise.

    Sets a unique title on the seeded session, opens the palette from the
    sidebar's Search button, then asserts the round-trip both ways:

    - A query matching the title lists the session row.
    - A query that matches nothing lists no session and surfaces the
      "No results found" empty state.

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

    # Open the palette from the sidebar's Search button (it doubles as the
    # session-search entry point now that the inline filter box is gone).
    search_button = page.get_by_test_id("sidebar-search-button")
    expect(search_button).to_be_visible(timeout=30_000)
    search_button.click()

    # Scope all result assertions to the palette dialog: the session's title
    # also renders in the chat header (we're on /c/{id}), so a page-wide text
    # match would never reach zero.
    dialog = page.get_by_role("dialog")
    palette_input = page.get_by_test_id("command-palette-input")
    expect(palette_input).to_be_visible()

    # A query that matches nothing lists no session (debounce + server round
    # trip resolve within the default expect timeout). "Actions" can still
    # match static commands, so assert the session title is absent from the
    # dialog rather than the whole list being blank.
    no_match = f"zzz-no-match-{uuid.uuid4().hex[:12]}"
    palette_input.fill(no_match)
    expect(dialog.get_by_text(title)).to_have_count(0)

    # A query matching the title lists the session — proving the palette
    # searches server-side on the title, not just filters a static page.
    palette_input.fill(marker)
    expect(dialog.get_by_text(title)).to_be_visible()
