"""Browser e2e for the Archived settings view's search box.

Settings → Archived (``/settings/archived``) offers a search box that narrows
the archived list server-side (``GET /v1/sessions?search_query=``). The server
matches a session when ``LOWER(title)`` or any of its items' ``search_text``
contains the query as a substring, so distinctive seeded titles are enough to
drive it. Typing is debounced, so the request trails the keystrokes; Playwright's
retrying assertions absorb that delay.

Search composes with the **Project** picker (both feed the same query), and a
query that matches nothing shows a search-specific empty state rather than the
plain "No archived sessions." copy.

These drive the real chain the ``SettingsPage`` unit tests mock out: seed
archived sessions over the REST API, load the view, and assert the filtered
list, the no-match empty state, and the restore-on-clear against the live
server. All seeded titles and project names carry a uuid suffix so the
assertions are immune to other tests' sessions on the shared server.
"""

from __future__ import annotations

import uuid

from playwright.sync_api import Page, expect

from tests.e2e_ui.sessions.test_archived_project_filter import (
    _delete_sessions,
    _seed_archived_session,
)

# The search Input carries no testid; its aria-label is the stable handle.
_SEARCH_LABEL = "Search archived sessions"


def test_archived_search_narrows_and_clears(page: Page, live_server: str) -> None:
    """Typing narrows the archived list to matching titles; clearing restores it.

    Seeds three archived sessions whose titles share a uuid prefix, two of them
    additionally sharing a ``keep`` marker. Searching the marker leaves exactly
    those two, a query that matches nothing shows the search empty state, and
    clearing the box brings all three back.
    """
    uniq = uuid.uuid4().hex[:6]
    titles = {
        "keep_one": f"e2e-archsearch-{uniq}-keep-one",
        "keep_two": f"e2e-archsearch-{uniq}-keep-two",
        "other": f"e2e-archsearch-{uniq}-other",
    }
    session_ids: list[str] = []
    try:
        for title in titles.values():
            session_ids.append(_seed_archived_session(live_server, title=title, project=None))

        page.goto(f"{live_server}/settings/archived")
        rows = page.get_by_test_id("archived-row")
        search = page.get_by_label(_SEARCH_LABEL)

        # Unfiltered: all three rows are present (newest by updated_at, so they
        # sort onto the first page even on a shared server).
        for title in titles.values():
            expect(rows.filter(has_text=title)).to_have_count(1)

        # → the "keep" marker: exactly the two matching rows survive. The uuid
        # makes the query unique server-wide, so the total count is exact.
        search.fill(f"{uniq}-keep")
        expect(rows).to_have_count(2)
        expect(rows.filter(has_text=titles["keep_one"])).to_have_count(1)
        expect(rows.filter(has_text=titles["keep_two"])).to_have_count(1)
        expect(rows.filter(has_text=titles["other"])).to_have_count(0)

        # → a query nothing can match: the search-specific empty state, not the
        # plain "no archived sessions" copy.
        search.fill(f"nomatch-{uuid.uuid4().hex}")
        expect(rows).to_have_count(0)
        expect(page.get_by_text("No archived sessions match.")).to_be_visible()

        # Clearing restores the unfiltered list.
        search.fill("")
        for title in titles.values():
            expect(rows.filter(has_text=title)).to_have_count(1)
    finally:
        _delete_sessions(live_server, session_ids)


def test_archived_search_composes_with_the_project_filter(
    page: Page,
    live_server: str,
) -> None:
    """Search narrows within the picked project instead of escaping it.

    Seeds two archived sessions in one project and one in another, all sharing a
    ``keep`` marker in their titles. Filtering to the first project and then
    searching the marker must leave only that project's rows — the other
    project's matching row stays hidden.
    """
    uniq = uuid.uuid4().hex[:6]
    proj_a = f"E2E Search Alpha {uniq}"
    proj_b = f"E2E Search Beta {uniq}"
    titles = {
        "a1": f"e2e-archsearchproj-{uniq}-keep-a1",
        "a2": f"e2e-archsearchproj-{uniq}-keep-a2",
        "b1": f"e2e-archsearchproj-{uniq}-keep-b1",
    }
    session_ids: list[str] = []
    try:
        session_ids.append(_seed_archived_session(live_server, title=titles["a1"], project=proj_a))
        session_ids.append(_seed_archived_session(live_server, title=titles["a2"], project=proj_a))
        session_ids.append(_seed_archived_session(live_server, title=titles["b1"], project=proj_b))

        page.goto(f"{live_server}/settings/archived")
        rows = page.get_by_test_id("archived-row")

        page.get_by_test_id("archived-project-filter").click()
        page.get_by_test_id(f"archived-project-option-{proj_a}").click()
        expect(rows).to_have_count(2)

        # The marker matches all three sessions, but only project A's are listed.
        page.get_by_label(_SEARCH_LABEL).fill(f"{uniq}-keep")
        expect(rows).to_have_count(2)
        expect(rows.filter(has_text=titles["b1"])).to_have_count(0)
    finally:
        _delete_sessions(live_server, session_ids)
