"""E2E: the optimistic user-message bubble lifecycle in the chat surface.

These browser tests drive the real SPA against a spawned server and
exercise the path a queued/optimistic user message takes:

    send → optimistic bubble renders immediately → server consumes it
    (``session.input.consumed``) → bubble promotes into committed
    history (not dropped, not duplicated) → survives navigation.

They guard the store wiring this change refactored — the
``session.input.consumed`` promotion in ``chatStore.handleSessionEvent``
and the ``bindStream`` snapshot hydration of ``pendingUserMessages``. A
regression in the promote path (dropping the bubble, double-rendering
it, or popping the wrong pending entry) turns these red.

Scope caveat — read before assuming these cover everything:

The ``pending_inputs`` server-side replay this change adds is
**native-terminal only** (claude-native / codex-native): only those
sessions defer persistence to the transcript forwarder and need the
in-memory replay to survive a rebind. The e2e_ui harness runs an
``openai-agents`` agent (``conftest._TEST_AGENT_YAML``) — native claude
needs the ``claude`` CLI binary + tmux, which this harness doesn't
provide. So on this agent the user message persists at POST time and is
re-loaded from ``items`` on navigation; the native ``pending_inputs``
replay itself is covered by the unit tests
(``tests/runtime/test_pending_inputs.py`` and the ``chatStore``
``session.input.consumed`` / ``bindStream`` suites). What these e2e
tests faithfully verify is the **client** lifecycle (optimistic render,
promote-without-drop-or-dup, queue-while-streaming, navigation
hydration) end-to-end through the real SPA.

User-message bubbles are ``data-testid="message-bubble"`` +
``data-role="user"`` (see ``ChatPage.tsx``). The user's own message
text is deterministic regardless of the LLM's reply, so assertions key
off unique sentinel strings — no dependence on model output.

The final test also covers the **"Queued" badge** (``data-testid=
"message-delivery-status"``): a message sent while a turn is in flight
carries ``queued`` on its optimistic pending entry, so the bubble shows
a "Queued" chip; when the agent picks the message up its
``session.input.consumed`` promotes the bubble into committed history
(which has no ``queued``), so the chip drops on its own — drop-on-pickup,
no extra client state. The badge is optimistic (client send-time state),
so it's observable in the natural in-flight window without gating the LLM.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

# Unique sentinels per test so a user bubble is unambiguously locatable
# and can't collide with the assistant's reply text. Worded so the model
# has no reason to echo them verbatim into its own bubble.
_NAV_MSG = "sentinel-nav-7f3a remember this exact phrase"
_PROMOTE_MSG = "sentinel-promote-91b2 keep this bubble"
_QUEUE_MSG_A = "sentinel-queue-a-4d1e first of two"
_QUEUE_MSG_B = "sentinel-queue-b-8c6f second of two"
# Badge test: A holds the turn open (blocked on the mock gate) so B,
# sent while A streams, is observably "Queued" before it's picked up.
_BADGE_MSG_A = "sentinel-badge-a-2f7d hold the turn open"
_BADGE_MSG_B = "sentinel-badge-b-3e9a queued behind the first"

_COMPOSER_PLACEHOLDER = "Ask the agent anything…"


def _user_bubble(page: Page, text: str):
    """Locator for the user-message bubble carrying ``text``."""
    return page.locator('[data-testid="message-bubble"][data-role="user"]').filter(has_text=text)


def _queued_badge(page: Page, text: str):
    """Locator for the "Queued" chip on the user bubble carrying ``text``."""
    return _user_bubble(page, text).locator('[data-testid="message-delivery-status"]')


def _send(page: Page, text: str) -> None:
    """Type ``text`` into the composer and click Send.

    Clicks the button by its accessible name ``Send`` — which is present
    only when the composer has a draft (while a turn streams with no
    draft the same button is the ``Interrupt`` square), so a successful
    click also confirms the draft registered.
    """
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def test_optimistic_user_bubble_renders_then_persists_through_consume(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Send a message: it renders immediately and stays through the turn.

    Two claims, both about the optimistic-bubble lifecycle:

    1. The user bubble appears right after Send — before any assistant
       output — proving the optimistic render (``pendingUserMessages``)
       fires without waiting on the server.
    2. After the assistant's reply completes (so the message was
       consumed), there is still **exactly one** user bubble with that
       text. A count of 0 means the ``session.input.consumed`` promotion
       dropped the bubble; a count of 2 means it appended a committed
       block without clearing the optimistic one (double-render — the
       exact symptom this change targets).
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, _PROMOTE_MSG)

    # (1) Optimistic render: visible well before the LLM replies.
    expect(_user_bubble(page, _PROMOTE_MSG)).to_be_visible(timeout=10_000)

    # Wait for the assistant turn to complete — a real assistant bubble
    # with non-whitespace text (not the "Working…" shimmer, which has a
    # different testid). This guarantees the consume + promote happened.
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_have_text(re.compile(r"\S"), timeout=60_000)

    # (2) Exactly one user bubble survived the promote — not dropped, not
    # duplicated.
    expect(_user_bubble(page, _PROMOTE_MSG)).to_have_count(1)


def test_user_message_survives_navigation_away_and_back(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Send a message, navigate away and back: the bubble re-renders.

    Mirrors the reported symptom ("navigate away and back, the message
    doesn't render until history loads"). After the turn completes we
    leave the conversation (``/`` landing) and return to ``/c/<id>``,
    forcing a cold re-hydration from the snapshot. The user bubble must
    re-render from server state — if it only existed in client-only
    optimistic state it would be gone after the round trip.

    On this (non-native) agent the message is re-loaded from ``items``;
    the native ``pending_inputs`` replay that hydrates an *un-consumed*
    message is unit-tested (see module docstring). This still guards the
    ``bindStream`` hydration path against a regression that drops
    re-rendered user bubbles.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, _NAV_MSG)
    expect(_user_bubble(page, _NAV_MSG)).to_be_visible(timeout=10_000)

    # Let the turn finish so the message is committed server-side before
    # we navigate (the durable state we expect to re-hydrate).
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_have_text(re.compile(r"\S"), timeout=60_000)

    # Navigate away to the landing route, then back into the chat.
    page.goto(f"{base_url}/")
    expect(page.get_by_placeholder(_COMPOSER_PLACEHOLDER)).to_have_count(0)
    page.goto(f"{base_url}/c/{session_id}")

    # Re-hydrated from the snapshot — exactly one bubble, no duplicate.
    expect(_user_bubble(page, _NAV_MSG)).to_have_count(1, timeout=30_000)
    expect(_user_bubble(page, _NAV_MSG)).to_be_visible()


def test_second_message_queued_while_first_streams_both_render(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Queue a second message while the first turn is active: both render.

    Sends A, then types B and clicks Send while A's turn is still in
    flight (the composer keeps a working Send button whenever it holds a
    draft — ``showInterruptButton = isWorking && !hasDraft``). Both user
    bubbles must render and, once everything settles, persist as exactly
    one bubble each.

    This exercises queueing two optimistic bubbles and promoting both as
    their ``session.input.consumed`` events arrive in order — a count
    other than 1 for either means the FIFO promotion dropped or
    duplicated a queued bubble.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, _QUEUE_MSG_A)
    # A's optimistic bubble is up; the turn is now (or about to be)
    # working. Queue B immediately by typing a draft and sending again.
    expect(_user_bubble(page, _QUEUE_MSG_A)).to_be_visible(timeout=10_000)
    _send(page, _QUEUE_MSG_B)
    expect(_user_bubble(page, _QUEUE_MSG_B)).to_be_visible(timeout=10_000)

    # Let both turns drain. Two distinct user messages were sent, so once
    # the session goes idle there must be exactly one bubble for each —
    # both consumed and promoted, neither dropped nor double-rendered.
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_have_text(re.compile(r"\S"), timeout=60_000)
    expect(_user_bubble(page, _QUEUE_MSG_A)).to_have_count(1, timeout=60_000)
    expect(_user_bubble(page, _QUEUE_MSG_B)).to_have_count(1, timeout=60_000)


def test_queued_badge_shows_then_drops_on_pickup(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A message queued behind a live turn shows "Queued", then it drops.

    Same setup as ``test_second_message_queued_while_first_streams_both_render``
    (send A, then send B while A's turn is still in flight), but asserting
    the delivery badge rather than just bubble survival:

    1. While B is queued behind A's active turn, B's optimistic bubble
       carries ``queued`` (set from the live ``status``/``sessionStatus``
       at submit time), so its "Queued" chip renders — the state the UI
       couldn't show before: a message visibly waiting behind a busy agent.
    2. Once both turns drain, B has been picked up and its
       ``session.input.consumed`` promotes it into committed history (which
       has no ``queued``), so the chip disappears on its own — drop-on-
       pickup, the whole point of the simplified design. No extra client
       state, no pickup signal.

    The badge is optimistic (driven by client send-time state), so step 1
    doesn't depend on gating the LLM — it's observable in the same natural
    in-flight window the sibling test relies on. Step 2's assertion waits
    out the turns, so it's robust even if the window in step 1 is brief.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    # Send A; as soon as its bubble is up the turn is (about to be) working.
    _send(page, _BADGE_MSG_A)
    expect(_user_bubble(page, _BADGE_MSG_A)).to_be_visible(timeout=10_000)

    # Queue B immediately, while A's turn is in flight. B's "Queued" chip
    # renders from the optimistic send-time state.
    _send(page, _BADGE_MSG_B)
    expect(_user_bubble(page, _BADGE_MSG_B)).to_be_visible(timeout=10_000)
    badge = _queued_badge(page, _BADGE_MSG_B)
    expect(badge).to_be_visible(timeout=10_000)
    expect(badge).to_have_text("Queued")

    # Let both turns drain. Once B is picked up its bubble promotes to
    # committed (no `queued`), so the "Queued" chip drops on its own.
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_have_text(re.compile(r"\S"), timeout=60_000)
    expect(_queued_badge(page, _BADGE_MSG_B)).to_have_count(0, timeout=60_000)
    # Both messages still render exactly once (sanity: no drop/dup).
    expect(_user_bubble(page, _BADGE_MSG_A)).to_have_count(1, timeout=60_000)
    expect(_user_bubble(page, _BADGE_MSG_B)).to_have_count(1)
