"""E2E: switching sessions changes routes in place without a document reload.

This covers the user-facing SPA navigation contract around ``/c/<session_id>``
routes. The React Router upgrade in this change set updates the client-side
routing layer and removes the legacy ``future`` flag from ``BrowserRouter``;
this test pins that navigating between two existing sessions through the
sidebar still updates the URL in place and preserves the current document.

No LLM turn is involved.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

_LOAD_MARKER_INIT_SCRIPT = """
window.__documentLoadMarker = crypto.randomUUID();
"""


def test_sidebar_session_switch_navigates_in_place(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Sidebar session picks should route to another conversation without reload.

    :param page: Fresh Playwright page.
    :param seeded_session_pair: ``(base_url, session_a, session_b)`` for two
        real sessions on the same live server.
    """
    base_url, session_a, session_b = seeded_session_pair
    page.add_init_script(_LOAD_MARKER_INIT_SCRIPT)

    page.goto(f"{base_url}/c/{session_a}")
    expect(page).to_have_url(f"{base_url}/c/{session_a}")
    load_marker = page.evaluate("window.__documentLoadMarker")

    page.locator(f'a[href="/c/{session_b}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}", timeout=30_000)
    assert page.evaluate("window.__documentLoadMarker") == load_marker

    page.locator(f'a[href="/c/{session_a}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_a}", timeout=30_000)
    assert page.evaluate("window.__documentLoadMarker") == load_marker
