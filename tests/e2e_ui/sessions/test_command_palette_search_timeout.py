"""E2E: the command palette tells a timed-out session search apart from a failure.

Session search fetches carry a 10 s client deadline (``SEARCH_FETCH_TIMEOUT_MS``
in ``web/src/hooks/useConversations.ts``). A search the server never answers
must show the timed-out guidance; an HTTP error keeps the generic message.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, Route, expect

_SLOW_TERM = "e2e slow search"
_BROKEN_TERM = "e2e broken search"
_TIMED_OUT = "Search timed out. Try a more specific search."
_GENERIC = "Couldn't load sessions."


def _stub_search(route: Route) -> None:
    """Stall the slow term, fail the broken term, and pass everything else through."""
    term = parse_qs(urlparse(route.request.url).query).get("search_query", [""])[0]
    if term == _SLOW_TERM:
        return  # Never answered, so only the client deadline can settle it.
    if term == _BROKEN_TERM:
        route.fulfill(status=500, json={"error": {"message": "search failed"}})
        return
    route.fallback()


def test_command_palette_search_timeout_and_failure_messages(page: Page, live_server: str) -> None:
    """A stalled search shows the timeout message; an HTTP 500 stays generic."""
    page.route("**/v1/sessions?*", _stub_search)
    page.goto(live_server)
    expect(page.get_by_role("heading", name="What should we build?")).to_be_visible(timeout=30_000)

    page.keyboard.press("ControlOrMeta+k")
    palette_input = page.get_by_test_id("command-palette-input")
    expect(palette_input).to_be_focused(timeout=10_000)
    dialog = page.get_by_role("dialog")

    palette_input.fill(_SLOW_TERM)
    expect(dialog.get_by_text(_TIMED_OUT)).to_be_visible(timeout=20_000)
    expect(dialog.get_by_text(_GENERIC)).to_have_count(0)

    # Non-timeout errors keep React Query's retries (~7 s) before settling.
    palette_input.fill(_BROKEN_TERM)
    expect(dialog.get_by_text(_GENERIC)).to_be_visible(timeout=20_000)
    expect(dialog.get_by_text(_TIMED_OUT)).to_have_count(0)
