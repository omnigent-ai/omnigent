"""E2E: author attribution badge visibility in solo vs shared sessions.

Guards the ``isSessionShared`` gate, the
``created_by`` persistence fix, and the avatar-badge UX
rules from the presence work (``designs/UI/PRESENCE.md``):

- Another contributor's message carries an avatar circle
  (``data-testid="message-author"``, initials on a per-user color)
  whose ``aria-label`` is the author's email — no email text is
  rendered inline.
- Your OWN messages never carry the badge: you know what you sent;
  the badge exists to attribute OTHER people's messages.

Scenario 1 – solo session (no identity):
  A browser context without ``X-Forwarded-Email`` receives
  ``{"user_id": "local"}`` from ``GET /v1/me``.  The frontend's
  ``getCurrentAuthorId()`` maps the reserved ``"local"`` sentinel to
  ``null``, so ``viewerId=null``, ``isSessionSharedWithOthers`` returns
  ``false``, and no ``data-testid="message-author"`` element is rendered.

Scenario 2 – shared session, author's own view vs a peer's view:
  The session is owned by ``"local"`` (created with no header). Alice
  and Bob are granted edit access as the ``"local"`` owner. Alice sends
  a message from her authenticated context: her own bubble must NOT
  carry the badge (self-exclusion), optimistic and committed alike.
  Bob's authenticated context then loads the session: Alice's committed
  bubble MUST carry the badge with her email as the accessible label —
  which also pins that ``created_by`` survived the persistence
  round-trip (the created_by persistence regression).

Scenario 3 – terminal-typed message (``external_conversation_item``):
  Posted as Alice, viewed by Bob — the badge must appear, pinning the
  ``_persist_external_conversation_item`` ``created_by`` fix.

These tests drive the full browser → SPA → server stack against a real
LLM.  They are excluded from the default ``pytest`` run and gated to
``workflow_dispatch`` CI, like the rest of the e2e_ui suite.

Selectors:
  - user bubbles: ``data-testid="message-bubble"`` + ``data-role="user"``
  - author badge: ``data-testid="message-author"`` (avatar beside the
    bubble; ``aria-label`` carries the author email)
  - composer:     placeholder ``"Ask the agent anything…"``
"""

from __future__ import annotations

import re
import uuid

import httpx
from playwright.sync_api import Browser, Page, expect

# Unique per test run to avoid cross-test pollution in a long-lived
# server process.
_RUN_TAG = uuid.uuid4().hex[:8]
_ALICE = f"alice-{_RUN_TAG}@example.com"
_BOB = f"bob-{_RUN_TAG}@example.com"

# Mirrors auth.py — the level granting edit access.
_LEVEL_EDIT = 2

_COMPOSER_PLACEHOLDER = "Ask the agent anything…"


def _grant_edit(base_url: str, session_id: str, grantee_email: str) -> None:
    """Grant ``grantee_email`` edit access to ``session_id`` as the ``"local"`` owner.

    The request carries no ``X-Forwarded-Email`` header, so the server
    reads the identity as the reserved ``"local"`` sentinel — the default
    owner of sessions created by the :func:`seeded_session` fixture.
    ``"local"`` holds ``LEVEL_OWNER``, which satisfies the manage-level
    check on ``PUT /v1/sessions/{id}/permissions``.  ``ensure_user`` in
    the server auto-creates the grantee account if it doesn't exist yet.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: Session to grant access to, e.g. ``"conv_abc123"``.
    :param grantee_email: Email of the user to grant edit access to,
        e.g. ``"alice@example.com"``.
    """
    httpx.put(
        f"{base_url}/v1/sessions/{session_id}/permissions",
        json={"user_id": grantee_email, "level": _LEVEL_EDIT},
        timeout=10.0,
    ).raise_for_status()


def _send(page: Page, text: str) -> None:
    """Type ``text`` into the composer and click Send.

    :param page: Playwright page.
    :param text: Message text to send.
    """
    composer = page.get_by_placeholder(_COMPOSER_PLACEHOLDER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _user_bubble(page: Page, text: str):
    """Locator for the committed-or-optimistic user bubble carrying ``text``.

    :param page: Playwright page.
    :param text: Sentinel text the bubble must contain.
    :returns: Playwright locator.
    """
    return page.locator('[data-testid="message-bubble"][data-role="user"]').filter(has_text=text)


def test_author_badge_hidden_in_solo_session(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Author badge is never rendered when there is no user identity.

    A failure here means one of:

    - ``isSessionSharedWithOthers`` is returning ``true`` even when
      ``viewerId`` is ``null`` — the gate is broken.
    - ``UserBubble`` renders ``data-testid="message-author"`` regardless
      of ``shouldShowAuthorBadge`` — a regression in the rendering logic.

    No ``X-Forwarded-Email`` header ⇒ ``GET /v1/me`` returns
    ``{"user_id": "local"}`` ⇒ ``getCurrentAuthorId()`` maps ``"local"``
    to ``null`` ⇒ ``viewerId=null`` ⇒
    ``isSessionSharedWithOthers("local", null, …)=false`` ⇒ no badge.

    :param page: Playwright page (no extra headers — default local identity).
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound
        session created by the fixture.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    marker = f"solo-author-{uuid.uuid4().hex[:8]}"
    _send(page, f"Say 'ack' in one word. {marker}")

    # Optimistic bubble renders before the LLM replies.
    expect(_user_bubble(page, marker)).to_be_visible(timeout=10_000)

    # No author badge on the optimistic bubble — isSessionShared=false.
    # Count=0 (not just hidden) proves the element is absent from the DOM,
    # not merely off-screen; a present-but-hidden badge would signal the
    # JS gate is broken, not just CSS.
    expect(_user_bubble(page, marker).get_by_test_id("message-author")).to_have_count(0)

    # Wait for the turn to complete so session.input.consumed fired and
    # the bubble is now committed.
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_have_text(re.compile(r"\S"), timeout=60_000)

    # Still no badge on the committed bubble.
    expect(_user_bubble(page, marker).get_by_test_id("message-author")).to_have_count(0)


def test_author_badge_skips_own_messages_and_marks_peers(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Own messages carry no badge; a peer sees the author's avatar.

    A failure on the no-badge-for-Alice assertions means the
    self-exclusion in ``shouldShowAuthorBadge`` regressed — users would
    see their own circle stamped on every message they send in a shared
    session (the exact thing the UX guidance forbids).

    A failure on Bob's badge assertion means one of:

    - ``session.input.consumed`` / persistence dropped ``created_by``,
      so the committed bubble has ``createdBy=undefined`` (the
      created_by persistence regression), or
    - the badge rendering lost the accessible author label (the email
      must be exposed via ``aria-label`` — it is no longer rendered as
      inline text).

    :param browser: Playwright session-scoped browser; dedicated contexts
        with Alice's and Bob's headers are created and closed here.
    :param seeded_session: ``(base_url, session_id)`` from the pre-created
        session fixture.
    """
    base_url, session_id = seeded_session
    _grant_edit(base_url, session_id, _ALICE)
    _grant_edit(base_url, session_id, _BOB)

    marker = f"shared-author-{uuid.uuid4().hex[:8]}"
    alice_ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": _ALICE})
    try:
        alice = alice_ctx.new_page()
        alice.goto(f"{base_url}/c/{session_id}")
        _send(alice, f"Say 'ack' in one word. {marker}")

        # Optimistic bubble renders immediately after Send — with NO
        # badge: author == viewer (Alice), and self is never badged.
        expect(_user_bubble(alice, marker)).to_be_visible(timeout=10_000)
        expect(_user_bubble(alice, marker).get_by_test_id("message-author")).to_have_count(0)

        # Wait for the turn to complete so session.input.consumed fired
        # and the optimistic bubble was promoted to committed history.
        assistant = alice.locator('[data-testid="message-bubble"][data-role="assistant"]').first
        expect(assistant).to_have_text(re.compile(r"\S"), timeout=60_000)

        # Still no badge on Alice's own committed bubble.
        expect(_user_bubble(alice, marker).get_by_test_id("message-author")).to_have_count(0)
    finally:
        alice_ctx.close()

    bob_ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": _BOB})
    try:
        bob = bob_ctx.new_page()
        bob.goto(f"{base_url}/c/{session_id}")

        # KEY ASSERTION: Bob (a different authenticated viewer) sees the
        # avatar badge on Alice's committed bubble, with her email as the
        # accessible label. This pins both the created_by persistence
        # round-trip and the foreign-author badge rendering.
        badge = _user_bubble(bob, marker).get_by_test_id("message-author")
        expect(badge).to_be_visible(timeout=15_000)
        expect(badge).to_have_attribute("aria-label", _ALICE)
    finally:
        bob_ctx.close()


def test_terminal_typed_message_shows_author_badge_to_peers(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Terminal-typed message carries the author badge for a peer viewer.

    Simulates the transcript forwarder posting ``external_conversation_item``
    on behalf of Alice — the event type produced when a user types directly
    in the native terminal (no pending-input entry is recorded first). Before
    the fix in ``_persist_external_conversation_item``, ``created_by`` was
    never passed for this event type, so items were stored with
    ``created_by=None`` and the author badge never appeared in the chat view.

    Bob (not Alice) is the viewer: the badge marks OTHER contributors,
    so the author's own view would render nothing by design.

    :param browser: Playwright browser session; a dedicated context with
        Bob's header is created and closed inside this test.
    :param seeded_session: ``(base_url, session_id)`` from the pre-created
        session fixture.
    """
    base_url, session_id = seeded_session
    _grant_edit(base_url, session_id, _ALICE)
    _grant_edit(base_url, session_id, _BOB)

    marker = f"terminal-direct-{uuid.uuid4().hex[:8]}"
    # POST as Alice, simulating the transcript forwarder authenticating
    # with her credentials when she types directly in the native terminal.
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"hi from terminal {marker}"}],
                },
                "response_id": f"resp_terminal_{uuid.uuid4().hex[:8]}",
            },
        },
        headers={"X-Forwarded-Email": _ALICE},
        timeout=10.0,
    ).raise_for_status()

    bob_ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": _BOB})
    try:
        bob = bob_ctx.new_page()
        bob.goto(f"{base_url}/c/{session_id}")

        # The committed bubble from external_conversation_item should
        # appear in the initial history load (no pending-input round-trip).
        bubble = _user_bubble(bob, marker)
        expect(bubble).to_be_visible(timeout=15_000)

        # The badge must be present with Alice's email as its accessible
        # label: sessionOwner="local" ≠ viewerId=Bob → shared, and the
        # author (Alice) differs from the viewer (Bob). Before the
        # created_by fix the badge was absent because created_by was None.
        badge = bubble.get_by_test_id("message-author")
        expect(badge).to_be_visible()
        expect(badge).to_have_attribute("aria-label", _ALICE)
    finally:
        bob_ctx.close()
