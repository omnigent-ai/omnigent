"""E2E: the too-many-tabs banner appears when open streams fill the HTTP pool.

Each tab with a conversation open holds one long-lived
``GET /v1/sessions/{id}/stream`` SSE request. Browsers cap HTTP/1.1
connections at ~6 per origin and share that budget across every tab in the
profile, so once ~6 conversations are open the held streams occupy every slot
and unrelated requests queue behind them — the app appears hung with nothing
explaining why. The banner makes that cause visible.

Why several pages in ONE browser context: Web Locks (which back the count) are
scoped to a browsing-context group, and pages in one Playwright context share a
lock manager — the same scope as real tabs in one browser profile. Separate
contexts are isolated from each other and would each count only themselves.

The same conversation opened N times is deliberate and realistic: every tab runs
its own store and stream pump, so N tabs on one session hold N streams and
consume N connections, exactly as N tabs on N sessions would.

A failure here means one of:

- The lock registry stopped counting held streams
  (``web/src/lib/streamTabRegistry.ts``), or ``startStreamPump`` stopped
  acquiring/releasing a slot for the stream's lifetime.
- The banner's threshold or render logic regressed
  (``web/src/components/StreamTabLimitBanner.tsx``), or it fell out of the
  standalone root in ``web/src/main.tsx``.
- The HTTP/1.1 gate started suppressing the banner on the local dev server
  (which serves HTTP/1.1, so the cap genuinely applies).
"""

from __future__ import annotations

from playwright.sync_api import Browser, expect

_COMPOSER = "Ask the agent anything…"
# Mirrors WARN_AT_TABS in web/src/components/StreamTabLimitBanner.tsx: the
# banner fires while the app still works, one slot before the pool is full.
_WARN_AT_TABS = 5


def test_banner_warns_once_open_tabs_threaten_the_connection_pool(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Opening enough conversation tabs surfaces the warning; closing clears it.

    :param browser: Playwright session-scoped browser. One context stands in
        for one browser profile, whose tabs share both the connection pool and
        the Web Locks scope.
    :param seeded_session: ``(base_url, session_id)`` from the fixture.
    """
    base_url, session_id = seeded_session
    context = browser.new_context()
    try:
        pages = []
        for _ in range(_WARN_AT_TABS):
            page = context.new_page()
            page.goto(f"{base_url}/c/{session_id}")
            # Wait for the composer before opening the next tab: the stream (and
            # so the lock) is only held once the conversation has actually bound.
            expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
            pages.append(page)

        # Assert on the last tab: its own slot acquisition refreshes the count
        # immediately, so it doesn't wait on the peer-tab poll interval.
        banner = pages[-1].get_by_role("status").filter(has_text="conversation open")
        expect(banner).to_be_visible(timeout=30_000)
        expect(banner).to_contain_text(f"{_WARN_AT_TABS} tabs have a conversation open")

        # Closing a tab releases its stream — and its connection — so the
        # warning must retire itself rather than persist after the user has
        # already acted on it.
        pages[0].close()
        expect(banner).not_to_be_visible(timeout=30_000)
    finally:
        context.close()
