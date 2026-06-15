"""Browser e2e for the sidebar's pin / unpin quick action.

Pinning is a client-side navigation preference (persisted to
``localStorage`` under ``omnigent:pinned-conversation-ids`` — see
``sidebarNav.ts``), surfaced as a hover/focus-revealed button on every
conversation row (``data-testid="quick-pin-conversation"``). Toggling it
moves the row between the sidebar's grouped sections:

  - **Pin** lifts the row out of "Recent" and into a "Pinned" section
    rendered above it (``ConversationList`` peels pinned, non-archived
    rows into their own group — Sidebar.tsx).
  - **Unpin** drops it back under "Recent".

These drive the real chain the ``Sidebar`` unit tests mock out: the live
``GET /v1/sessions`` list feeding ``useConversations`` → the section
split → the row landing under the right header. The unit tests render
the sidebar with a hand-mocked list; here the rows come from a real
server so a regression in the list shape, the owner-vs-shared split, or
the pinned peel would surface end-to-end.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give a session a title via ``PATCH /v1/sessions/{id}``.

    The seeded session has no title (renders as "New session"), which is
    ambiguous when other tests' sessions accumulate in the shared server.
    A unique title makes the row trivially identifiable in assertions
    even though these tests locate it by its stable ``/c/{id}`` href.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id to rename.
    :param title: The new title to set.
    """
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _section(page: Page, title: str) -> Locator:
    """Locate the sidebar ``<section>`` whose header reads *title*.

    Each ``ConversationSection`` renders an ``<h2>`` with a collapse
    button whose accessible name is the section title (e.g. "Pinned",
    "Recent"). Scoping row assertions to the matching section is how we
    prove a row is grouped under the right header.

    :param page: Playwright page with the sidebar open.
    :param title: Section header text, e.g. ``"Pinned"``.
    :returns: A locator for the section element.
    """
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def test_pin_moves_session_to_pinned_section(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Pinning a Recent session lifts its row into the Pinned section.

    Failure modes this catches that the mocked unit test can't:

    - The live ``GET /v1/sessions`` row shape drifts so the owner split
      drops the session out of "Recent" (it would never be pinnable).
    - The pin toggle persists but the section peel regresses, leaving the
      row under "Recent" after a pin.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-pin-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()
    # Owned, non-archived, unpinned → starts under "Recent", never
    # "Pinned" (no Pinned section exists yet).
    expect(_section(page, "Recent").locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)

    # Pin via the row's quick action. Hover first so the desktop
    # hover-revealed control is interactable.
    row.hover()
    pin_button = row.get_by_test_id("quick-pin-conversation")
    expect(pin_button).to_have_attribute("aria-label", "Pin conversation")
    pin_button.click()

    # The row now lives under "Pinned" and out of "Recent", and the
    # quick action flips to its unpin affordance — both prove the toggle
    # ran through the sidebar's pin state, not a local no-op.
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(_section(page, "Recent").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)
    expect(_row(page, session_id).get_by_test_id("quick-pin-conversation")).to_have_attribute(
        "aria-label", "Unpin conversation"
    )


def test_unpin_moves_session_back_to_recent(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Unpinning a pinned session drops its row back under Recent.

    Pins first (so there's something to unpin), confirms the row is under
    "Pinned", then unpins and asserts it returns to "Recent" and the
    "Pinned" section no longer holds it. Catches a regression where the
    toggle is one-way (pin sticks, unpin no-ops) or the section peel
    fails to re-home the row.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-unpin-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()

    # Pin it.
    row.hover()
    row.get_by_test_id("quick-pin-conversation").click()
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_id}"]')).to_be_visible()

    # Now unpin from under the Pinned header.
    pinned_row = (
        _section(page, "Pinned")
        .locator("li")
        .filter(has=page.locator(f'a[href="/c/{session_id}"]'))
    )
    pinned_row.hover()
    unpin_button = pinned_row.get_by_test_id("quick-pin-conversation")
    expect(unpin_button).to_have_attribute("aria-label", "Unpin conversation")
    unpin_button.click()

    # Back under "Recent", and no longer in "Pinned".
    expect(_section(page, "Recent").locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)
