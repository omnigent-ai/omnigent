"""E2E: switching back to an already-open conversation shows no loading state.

Keeping a conversation's stream open in the background (#4113) means an in-app
switch back to a conversation you have already visited paints instantly from
its live in-memory entry — no snapshot re-hydration, no "Loading conversation…"
placeholder. Before that change every switch tore the stream down and
re-hydrated from scratch, flashing the placeholder each time.

The test seeds two conversations, opens A, switches to B, then switches back to
A via the sidebar — an in-app ``switchTo``, NOT a full ``page.goto`` reload,
because only in-app navigation gets the keep-open behavior. On the return it
asserts two things:

  - the ``HydratingPlaceholder`` never renders, and
  - A's history is not re-fetched (``GET /v1/sessions/{A}/items``).

The second is the network signature of the loading state and the deterministic
half of the check: ``loadingConversation`` (which gates the placeholder) is set
exactly while ``bindStream`` re-hydrates, and ``bindStream`` is the only thing
that GETs ``/items``. So "no items re-fetch on the return" implies "no loading
state" without depending on catching a sub-second DOM flash.

A failure means one of:

  - ``chatStore.switchTo`` stopped treating an already-live conversation as live
    (its ``wasLive`` fast path), so it re-binds and re-hydrates on every switch; or
  - the conversation's background stream is being torn down on switch-away, so
    returning to it is a cold load again.
"""

from __future__ import annotations

import re
import uuid

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn

_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'


def test_switching_back_to_open_conversation_shows_no_loading_state(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Returning to a backgrounded conversation must not show a loading state.

    :param page: Playwright page fixture.
    :param seeded_session_pair: ``(base_url, session_a, session_b)`` — two
        runner-bound sessions in one server, created by the fixture.
    """
    base_url, session_a, session_b = seeded_session_pair
    marker_a = f"conv-a-{uuid.uuid4().hex[:8]}"
    marker_b = f"conv-b-{uuid.uuid4().hex[:8]}"
    # Committed transcripts written straight to the store (no runner/LLM), so
    # each conversation is non-empty and its own message can be asserted visible.
    seed_committed_turn(session_a, prompt=marker_a, reply="reply from a")
    seed_committed_turn(session_b, prompt=marker_b, reply="reply from b")

    # Record A's history re-fetches for the whole run; we only inspect the ones
    # that fire during the switch back (indexed below).
    a_items_re = re.compile(rf"/v1/sessions/{re.escape(session_a)}/items")
    a_item_fetches: list[str] = []
    page.on(
        "request",
        lambda r: a_item_fetches.append(r.url) if a_items_re.search(r.url) else None,
    )

    placeholder = page.get_by_test_id("hydrating-placeholder")
    bubble_a = page.locator(_USER_BUBBLE, has_text=marker_a)
    bubble_b = page.locator(_USER_BUBBLE, has_text=marker_b)

    # Cold-open A (first visit — a loading state HERE is expected and allowed).
    page.goto(f"{base_url}/c/{session_a}")
    expect(bubble_a).to_be_visible(timeout=15_000)

    # Switch to B in-app via its sidebar link (a real switchTo, not a reload).
    page.locator(f'a[href="/c/{session_b}"]').click()
    page.wait_for_url(re.compile(rf"/c/{re.escape(session_b)}"))
    expect(bubble_b).to_be_visible(timeout=15_000)

    # Switch BACK to A. A stayed live in the background, so this must paint
    # instantly — no placeholder, no re-hydration.
    fetches_before = len(a_item_fetches)
    page.locator(f'a[href="/c/{session_a}"]').click()
    page.wait_for_url(re.compile(rf"/c/{re.escape(session_a)}"))

    # A's transcript is already there, painted from the live entry...
    expect(bubble_a).to_be_visible()
    # ...the loading placeholder never rendered...
    expect(placeholder).to_have_count(0)
    # ...and no history re-fetch fired for the return. Settle a beat first so a
    # regression's cold-load /items GET has time to show up and fail loudly.
    page.wait_for_timeout(500)
    assert len(a_item_fetches) == fetches_before, (
        "switching back to an already-open conversation re-fetched its history "
        f"({a_item_fetches[fetches_before:]}); it should paint from the live "
        "stream with no loading state"
    )
