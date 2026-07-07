"""Browser e2e for assigning a color to a session from the sidebar.

The row kebab's "Color" submenu sets a reserved ``omni_color`` label via
``PATCH /v1/sessions/{id}`` (the same label mechanism as "Move to project").
The color is persisted server-side, so it must survive a full page reload —
that's the regression this guards: a color that only patched the in-memory
TanStack cache (and is lost on reload) would pass the ``Sidebar`` unit tests,
which mock the mutation, but fail here.

We assert persistence two ways after a reload: the row re-renders with its
``data-session-color`` from a fresh ``GET /v1/sessions``, and the server's own
``GET /v1/sessions/{id}`` snapshot returns the ``omni_color`` label.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Locator, Page, expect


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def test_session_color_is_preserved(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Setting a color via the kebab persists across a reload and on the server.

    Failure modes this catches that the mocked unit test can't:

    - The PATCH never fires (or 4xxs on wire drift) so the tint reverts on reload.
    - The color only patches the client cache and is lost once the sidebar
      refetches ``GET /v1/sessions`` after a reload.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(row).not_to_have_attribute("data-session-color", "blue")

    # Open the row kebab, open the Color submenu, and pick Blue. Hover first so
    # the desktop hover-revealed kebab trigger is interactable, and hover the
    # submenu trigger so its flyout opens before clicking the swatch.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("set-color-conversation").hover()
    page.get_by_test_id("color-blue").click()

    # The row reflects the color once the mutation's list refetch resolves.
    expect(row).to_have_attribute("data-session-color", "blue")

    # Reload: the sidebar refetches GET /v1/sessions from scratch. A color that
    # only lived in the client cache would revert here.
    page.reload()
    expect(_row(page, session_id)).to_have_attribute("data-session-color", "blue")

    # And the server agrees — the color was persisted as a label, not just rendered.
    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    labels = snap.json().get("labels") or {}
    assert labels.get("omni_color") == "blue", (
        f"server should persist the color label, got labels={labels!r}"
    )
