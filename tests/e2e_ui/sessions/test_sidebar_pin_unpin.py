"""Browser e2e for the sidebar's pin / unpin quick action.

Pinning is a client-side navigation preference (persisted to
``localStorage`` under ``omnigent:pinned-conversation-ids`` — see
``sidebarNav.ts``), surfaced as a hover/focus-revealed button on every
conversation row (``data-testid="quick-pin-conversation"``). Toggling it
moves the row between the sidebar's grouped sections:

  - **Pin** lifts the row out of "Chats" and into a "Pinned" section
    rendered above it (``ConversationList`` peels pinned, non-archived
    rows into their own group — Sidebar.tsx).
  - **Unpin** drops it back under "Chats".

These drive the real chain the ``Sidebar`` unit tests mock out: the live
``GET /v1/sessions`` list feeding ``useConversations`` → the section
split → the row landing under the right header. The unit tests render
the sidebar with a hand-mocked list; here the rows come from a real
server so a regression in the list shape, the owner-vs-shared split, or
the pinned peel would surface end-to-end.
"""

from __future__ import annotations

import time
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
    "Chats"). Scoping row assertions to the matching section is how we
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
    """Pinning a Chats session lifts its row into the Pinned section.

    Failure modes this catches that the mocked unit test can't:

    - The live ``GET /v1/sessions`` row shape drifts so the owner split
      drops the session out of "Chats" (it would never be pinnable).
    - The pin toggle persists but the section peel regresses, leaving the
      row under "Chats" after a pin.

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
    # Owned, non-archived, unpinned → starts under "Chats", never
    # "Pinned" (no Pinned section exists yet).
    expect(_section(page, "Chats").locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)

    # Pin via the row's quick action. Hover first so the desktop
    # hover-revealed control is interactable.
    row.hover()
    pin_button = row.get_by_test_id("quick-pin-conversation")
    expect(pin_button).to_have_attribute("aria-label", "Pin conversation")
    pin_button.click()

    # The row now lives under "Pinned" and out of "Chats", and the
    # quick action flips to its unpin affordance — both prove the toggle
    # ran through the sidebar's pin state, not a local no-op.
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(_section(page, "Chats").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)
    expect(_row(page, session_id).get_by_test_id("quick-pin-conversation")).to_have_attribute(
        "aria-label", "Unpin conversation"
    )


def test_unpin_moves_session_back_to_recent(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Unpinning a pinned session drops its row back under Chats.

    Pins first (so there's something to unpin), confirms the row is under
    "Pinned", then unpins and asserts it returns to "Chats" and the
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

    # Back under "Chats", and no longer in "Pinned".
    expect(_section(page, "Chats").locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)


def _pinned_session_order(page: Page) -> list[str]:
    """Return the ``/c/{id}`` hrefs under "Pinned" in top-to-bottom DOM order.

    ``evaluate_all`` reads the rows in render order, so the index of a
    session's href is its visible rank in the Pinned section — exactly what
    the ordering assertions compare.

    :param page: Playwright page with the sidebar open.
    :returns: Conversation hrefs (e.g. ``["/c/conv_b", "/c/conv_a"]``).
    """
    links = _section(page, "Pinned").locator('a[href^="/c/"]')
    return links.evaluate_all("els => els.map((e) => e.getAttribute('href'))")


def _updated_at(base_url: str, session_id: str) -> int:
    """Read a session's server-side ``updated_at`` from ``GET /v1/sessions``."""
    resp = httpx.get(f"{base_url}/v1/sessions", timeout=10.0)
    resp.raise_for_status()
    for item in resp.json()["data"]:
        if item["id"] == session_id:
            return int(item["updated_at"])
    raise AssertionError(f"session {session_id} not present in /v1/sessions")


def _bump_updated_at_past(base_url: str, session_id: str, reference_id: str) -> None:
    """Make *session_id* the most-recently-updated session, strictly past *reference_id*.

    ``PATCH /v1/sessions`` stamps ``updated_at = now_epoch()`` at
    whole-second granularity, so a single title bump issued in the same
    wall-clock second as *reference_id*'s last update can tie. Re-bump across
    a second boundary until the target is unambiguously newest — that is the
    update-time ordering the pinned-order assertion must refuse to follow.

    :param base_url: Spawned server base URL.
    :param session_id: The session to bump.
    :param reference_id: The session it must end up strictly newer than.
    :raises AssertionError: If it cannot get strictly ahead within the budget.
    """
    deadline = time.monotonic() + 15.0
    attempt = 0
    while time.monotonic() < deadline:
        _set_title(base_url, session_id, f"e2e-bump-{attempt}-{uuid.uuid4().hex[:8]}")
        if _updated_at(base_url, session_id) > _updated_at(base_url, reference_id):
            return
        attempt += 1
        time.sleep(1.1)
    raise AssertionError(f"could not bump {session_id} past {reference_id}'s updated_at")


def test_pinned_section_orders_by_pin_time_not_update_time(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Pinned rows hold their pin order even when a newer message arrives.

    The Pinned section orders strictly by when each session was pinned
    (oldest pin on top, newest pin at the bottom), not by ``updated_at`` like
    the Chats / Shared / Archived sections. The regression this guards: the
    Pinned group reused the ``updated_at``-desc comparator, so a pinned
    session jumped the moment it got a new message.

    The test pins ``a`` then ``b`` (so ``b`` is the newer pin and belongs at
    the bottom), then bumps ``b``'s ``updated_at`` to be the freshest of the
    two. ``a`` is the active chat so its sort key is frozen, leaving ``b`` as
    the unambiguously most-recently-updated row — under the old comparator
    that would lift ``b`` to the top. Asserting ``b`` stays at the bottom
    proves the pinned order follows pin time, not update time.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session_pair: ``(base_url, session_a, session_b)`` — two
        runner-bound sessions in the same server.
    """
    base_url, session_a, session_b = seeded_session_pair
    _set_title(base_url, session_a, f"e2e-order-A-{uuid.uuid4().hex[:8]}")
    _set_title(base_url, session_b, f"e2e-order-B-{uuid.uuid4().hex[:8]}")

    # View a: it becomes the active chat, so its sidebar sort key is frozen
    # and only b's updated_at moves when we bump it below.
    page.goto(f"{base_url}/c/{session_a}")

    # Pin a first, then b. The newer pin (b) belongs at the bottom of the
    # Pinned group, below the older pin (a).
    row_a = _row(page, session_a)
    expect(row_a).to_be_visible()
    row_a.hover()
    row_a.get_by_test_id("quick-pin-conversation").click()
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_a}"]')).to_be_visible()

    row_b = _row(page, session_b)
    row_b.hover()
    row_b.get_by_test_id("quick-pin-conversation").click()
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_b}"]')).to_be_visible()

    # Oldest pin (a) sits above the newer pin (b).
    order = _pinned_session_order(page)
    assert order.index(f"/c/{session_a}") < order.index(f"/c/{session_b}"), (
        f"newest pin should sort last, got {order}"
    )

    # Bump b so it is the most-recently-updated session, then reload to
    # refetch the list (pins live in localStorage and survive the reload).
    _bump_updated_at_past(base_url, session_b, session_a)
    page.reload()

    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_a}"]')).to_be_visible()
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_b}"]')).to_be_visible()

    # b is now the freshest session, but it must stay below a: pinned order is
    # by pin time. Under the old updated_at-desc comparator b would jump up.
    order_after = _pinned_session_order(page)
    assert order_after.index(f"/c/{session_a}") < order_after.index(f"/c/{session_b}"), (
        f"pinned order must follow pin time, not the bumped updated_at, got {order_after}"
    )
