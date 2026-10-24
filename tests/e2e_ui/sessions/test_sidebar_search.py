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

The ranking tests pin the result order the palette renders: sessions whose
title matches come before newer sessions that match only in chat content,
on the desktop palette and on the phone-width full-screen sheet.
"""

from __future__ import annotations

import re
import time
import uuid

import httpx
import pytest
from playwright.sync_api import Page, ViewportSize, expect

# Below the ``md`` breakpoint, so the palette opens as the full-screen sheet.
_PHONE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}


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


def _seed_title_and_content_matches(
    base_url: str, title_session_id: str, content_session_ids: tuple[str, str], marker: str
) -> list[str]:
    """
    Title one session with ``marker``; mention it only in two newer chats.

    :returns: Expected palette row titles in ranked order: the title match,
        then the content-only matches newest first.
    """
    title_match = f"Release plan {marker}"
    httpx.patch(
        f"{base_url}/v1/sessions/{title_session_id}", json={"title": title_match}, timeout=10.0
    ).raise_for_status()
    content_titles = ["Sprint retro notes", "Weekly team sync"]
    for index, (session_id, title) in enumerate(
        zip(content_session_ids, content_titles, strict=True)
    ):
        if index:
            # Distinct whole-second timestamps keep the newest-first order exact.
            time.sleep(1.1)
        httpx.patch(
            f"{base_url}/v1/sessions/{session_id}", json={"title": title}, timeout=10.0
        ).raise_for_status()
        httpx.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={
                "type": "external_assistant_message",
                "data": {
                    "agent": "hello_world",
                    "text": f"Remember the {marker} checklist before Friday.",
                },
            },
            timeout=10.0,
        ).raise_for_status()
    deadline = time.monotonic() + 30
    while True:
        response = httpx.get(
            f"{base_url}/v1/sessions",
            params={"search_query": marker, "visibility": "all"},
            timeout=10.0,
        )
        response.raise_for_status()
        found = response.json()["data"]
        if len(found) == 3 or time.monotonic() > deadline:
            break
        time.sleep(0.5)
    assert len(found) == 3, f"seeded sessions not searchable: {found}"
    return [title_match, *reversed(content_titles)]


def _expect_ranked_rows(page: Page, marker: str, ranked_titles: list[str]) -> None:
    """Type ``marker`` into the open palette and check the session row order."""
    palette_input = page.get_by_test_id("command-palette-input")
    expect(palette_input).to_be_visible()
    palette_input.fill(marker)
    rows = page.get_by_role("dialog").get_by_role("option")
    expect(rows).to_have_text([re.compile(re.escape(title)) for title in ranked_titles])
    # Content-only rows explain the match with a snippet; the title row does not.
    expect(rows.nth(0)).not_to_contain_text("checklist before Friday")
    for content_row in (rows.nth(1), rows.nth(2)):
        expect(content_row).to_contain_text(f"{marker} checklist")


def test_search_lists_title_matches_before_content_matches(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """An older title match is listed above newer content-only matches."""
    base_url, title_session_id = seeded_session
    _, *content_session_ids = seeded_session_pair
    marker = uuid.uuid4().hex[:10]
    ranked_titles = _seed_title_and_content_matches(
        base_url, title_session_id, tuple(content_session_ids), marker
    )

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{title_session_id}")
    search_button = page.get_by_test_id("sidebar-search-button")
    expect(search_button).to_be_visible(timeout=30_000)
    search_button.click()
    _expect_ranked_rows(page, marker, ranked_titles)


def test_phone_search_sheet_lists_title_matches_first(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """The phone-width search sheet, opened from the drawer, keeps the ranking."""
    base_url, title_session_id = seeded_session
    _, *content_session_ids = seeded_session_pair
    marker = uuid.uuid4().hex[:10]
    ranked_titles = _seed_title_and_content_matches(
        base_url, title_session_id, tuple(content_session_ids), marker
    )

    page: Page = request.getfixturevalue("page")
    page.set_viewport_size(_PHONE_VIEWPORT)
    page.goto(f"{base_url}/c/{title_session_id}")
    open_sidebar = page.get_by_role("button", name="Open sidebar")
    expect(open_sidebar).to_be_visible(timeout=30_000)
    open_sidebar.click()
    search_button = page.get_by_test_id("sidebar-search-button")
    expect(search_button).to_be_visible()
    search_button.click()
    expect(page.get_by_role("button", name="Close search")).to_be_visible()
    _expect_ranked_rows(page, marker, ranked_titles)
