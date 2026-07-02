"""E2E: two browser contexts on one session sync messages in real time.

Two independent browser contexts (owner + collaborator) open the same
``/c/<id>`` URL. The collaborator loads *before* the owner sends, so its
initial snapshot has no message — anything it later renders must have
arrived over the live ``GET /v1/sessions/{id}/stream`` SSE stream, not a
reload. The owner sends a uniquely-marked prompt; the test asserts the
collaborator sees the owner's user bubble appear without reloading, and
that the streamed assistant reply lands in both contexts.

This exercises the cross-client path the single-context smoke test
can't: the server broadcasts ``session.input.consumed`` to every
subscriber, and ``chatStore.handleSessionEvent`` promotes a peer's
consumed event into a user bubble when the local optimistic FIFO is
empty (``web/src/store/chatStore.ts`` ``session_input_consumed``
branch). If either the broadcast or that promotion regresses, the
collaborator never renders the owner's message and this test goes red.

Scope note: only conversation messages broadcast on the session stream.
Comment changes sync over a different channel — the per-session
comments fingerprint on ``WS /v1/sessions/updates`` — and are covered
by ``test_comments_realtime.py``. Filesystem changes still have no live
cross-client sync, so file sync stays out of scope here.

Selectors mirror ``test_smoke``: the composer by placeholder, real
message bubbles by ``data-testid="message-bubble"`` + ``data-role``
(the ``working-indicator`` shimmer is excluded by the role attribute).
"""

from __future__ import annotations

import re
import uuid

import httpx
from playwright.sync_api import Browser, expect


def test_two_browser_contexts_sync_message_realtime(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Owner's sent message appears live in a second context, no reload.

    A failure means one of:

    - The server stopped broadcasting ``session.input.consumed`` to
      peer stream subscribers (server regression in the events route /
      ``session_stream`` publish).
    - The SPA stopped promoting a peer's consumed event into a user
      bubble when its optimistic FIFO is empty (``chatStore``
      ``session_input_consumed`` regression — the cross-client branch
      at the empty-FIFO fallback).
    - The collaborator never subscribed to the live stream on session
      bind (``chatStore.switchTo`` regression).
    - The LLM never responded (Databricks credentials / model
      availability), which only affects the assistant-bubble checks.

    :param browser: Playwright session-scoped browser; two independent
        contexts stand in for two users sharing the session URL.
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound
        session, created by the fixture.
    """
    base_url, session_id = seeded_session
    # Unique per run so the bubble locator can't match leftover or
    # streamed-reply text — it has to be the user message we sent.
    marker = f"collab-sync-{uuid.uuid4().hex[:8]}"

    owner_ctx = browser.new_context()
    collab_ctx = browser.new_context()
    try:
        owner = owner_ctx.new_page()
        collab = collab_ctx.new_page()

        owner.goto(f"{base_url}/c/{session_id}")
        collab.goto(f"{base_url}/c/{session_id}")

        # Both composers visible ⇒ both pages booted and bound the
        # session stream. The collaborator must be subscribed before the
        # owner sends, otherwise a passing collaborator assertion could
        # be a post-reload snapshot read rather than a live delivery.
        owner_composer = owner.get_by_placeholder("Ask the agent anything…")
        collab_composer = collab.get_by_placeholder("Ask the agent anything…")
        expect(owner_composer).to_be_visible()
        expect(collab_composer).to_be_visible()

        owner_composer.fill(f"Reply with the single word pong. {marker}")
        owner.get_by_role("button", name="Send", exact=True).click()

        # Owner sees its own (optimistic) user bubble immediately.
        owner_user = owner.locator(
            '[data-testid="message-bubble"][data-role="user"]',
            has_text=marker,
        )
        expect(owner_user).to_be_visible()

        # KEY ASSERTION: the collaborator — which never reloaded and had
        # an empty transcript at subscribe time — renders the owner's
        # user bubble. The marker can only be here if it arrived over the
        # live SSE stream. 30s covers broadcast + persist + render under
        # cold-start routing without masking a true stall.
        collab_user = collab.locator(
            '[data-testid="message-bubble"][data-role="user"]',
            has_text=marker,
        )
        expect(collab_user).to_be_visible(timeout=30_000)

        # The streamed assistant reply reaches both subscribers. Match
        # any non-whitespace so an empty bubble (reducer fired, produced
        # no text) still fails. 60s covers LLM latency, as in test_smoke.
        owner_assistant = owner.locator(
            '[data-testid="message-bubble"][data-role="assistant"]'
        ).first
        collab_assistant = collab.locator(
            '[data-testid="message-bubble"][data-role="assistant"]'
        ).first
        expect(owner_assistant).to_have_text(re.compile(r"\S"), timeout=60_000)
        expect(collab_assistant).to_have_text(re.compile(r"\S"), timeout=60_000)
    finally:
        owner_ctx.close()
        collab_ctx.close()


def test_presence_circles_track_other_viewers(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Each viewer sees the OTHER user's presence circle; leaves clear it.

    Two authenticated contexts (Alice, Bob — granted edit by the
    ``"local"`` owner) open the same session. Presence rides the same
    ``GET /v1/sessions/{id}/stream`` SSE the message sync uses: opening
    the page registers the viewer server-side, the join broadcasts a
    full-state ``session.presence`` event, and the header renders
    ``data-testid="presence-avatar-<email>"`` circles for everyone but
    the viewer themself.

    A failure means one of:

    - The stream route stopped registering viewers (presence.connect
      wiring) or the snapshot-on-connect stopped carrying the list.
    - The SPA's ``session_presence`` handling or the self-filter in
      ``PresenceAvatars`` regressed (e.g. showing your own circle).
    - The leave path regressed: closing Bob's context must clear his
      circle for Alice once the server's ~15s leave-grace window (which
      absorbs reconnect churn) expires.

    No LLM turn is involved — presence is pure connection lifecycle.

    :param browser: Playwright session-scoped browser; two authenticated
        contexts stand in for two users sharing the session URL.
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound
        session, created by the fixture.
    """
    base_url, session_id = seeded_session
    run_tag = uuid.uuid4().hex[:8]
    alice_email = f"alice-presence-{run_tag}@example.com"
    bob_email = f"bob-presence-{run_tag}@example.com"
    for grantee in (alice_email, bob_email):
        # Grant as the "local" owner (no header) — level 2 = edit.
        httpx.put(
            f"{base_url}/v1/sessions/{session_id}/permissions",
            json={"user_id": grantee, "level": 2},
            timeout=10.0,
        ).raise_for_status()

    alice_ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": alice_email})
    bob_ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": bob_email})
    try:
        alice = alice_ctx.new_page()
        bob = bob_ctx.new_page()
        alice.goto(f"{base_url}/c/{session_id}")
        bob.goto(f"{base_url}/c/{session_id}")

        # Each sees the OTHER's circle. 15s covers page boot + stream
        # bind + the join broadcast round-trip on a cold server.
        expect(alice.get_by_test_id(f"presence-avatar-{bob_email}")).to_be_visible(timeout=15_000)
        expect(bob.get_by_test_id(f"presence-avatar-{alice_email}")).to_be_visible(timeout=15_000)

        # Self-filter: your own circle never renders in your own header.
        # Count=0 proves absence from the DOM, not just invisibility.
        expect(alice.get_by_test_id(f"presence-avatar-{alice_email}")).to_have_count(0)
        expect(bob.get_by_test_id(f"presence-avatar-{bob_email}")).to_have_count(0)

        # Hovering a circle shows the viewer's name IN THE VIEWPORT.
        # The in-viewport check is load-bearing: when the trigger ref is
        # dropped (React 18 + a non-forwardRef component under
        # TooltipTrigger asChild), Radix still mounts the tooltip with
        # the right text but anchors it at the page origin, off-screen —
        # a DOM-presence assertion passes while users see nothing.
        alice.get_by_test_id(f"presence-avatar-{bob_email}").hover()
        name_tooltip = alice.locator("[data-slot=tooltip-content]", has_text=bob_email)
        expect(name_tooltip).to_be_visible(timeout=5_000)
        expect(name_tooltip).to_be_in_viewport()

        # Bob leaves. His circle must clear for Alice only after the
        # server's leave-grace window expires (the spawned test server runs
        # with _LEAVE_GRACE_S patched to 1s; see live_server. Prod is 15s to
        # absorb ingress reconnect churn) — 15s bounds grace + broadcast +
        # render without masking a true ghost-viewer stall.
        bob_ctx.close()
        expect(alice.get_by_test_id(f"presence-avatar-{bob_email}")).to_have_count(
            0, timeout=15_000
        )
    finally:
        alice_ctx.close()
        bob_ctx.close()


def test_presence_idle_greys_backgrounded_viewer(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A viewer whose tab is backgrounded ≥30s greys out for co-viewers.

    Bob's page gets ``document.hidden`` forced to ``true`` and a
    ``visibilitychange`` dispatched — the SPA's presence tracker starts
    its 30s debounce, then reconnects the session stream with
    ``?idle=true``; the server flips Bob's aggregate and broadcasts, and
    Alice's header dims his circle (the ``opacity-40`` class). Restoring
    visibility must un-grey him promptly (the active flip skips the
    debounce and reconnects immediately).

    This is the only end-to-end exercise of the idle uplink — unit
    tests cover the tracker and the registry aggregate separately, but
    not the full reconnect-carries-the-flag loop through a real
    browser, server, and SSE stream.

    A failure means one of: the stream URL stopped carrying ``idle``,
    the per-attempt reconnect machinery regressed (no flip ever reaches
    the server), the registry stopped broadcasting aggregate changes,
    or PresenceAvatars stopped dimming idle viewers.

    :param browser: Playwright session-scoped browser.
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound
        session, created by the fixture.
    """
    base_url, session_id = seeded_session
    run_tag = uuid.uuid4().hex[:8]
    alice_email = f"alice-idle-{run_tag}@example.com"
    bob_email = f"bob-idle-{run_tag}@example.com"
    for grantee in (alice_email, bob_email):
        httpx.put(
            f"{base_url}/v1/sessions/{session_id}/permissions",
            json={"user_id": grantee, "level": 2},
            timeout=10.0,
        ).raise_for_status()

    alice_ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": alice_email})
    bob_ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": bob_email})
    try:
        alice = alice_ctx.new_page()
        bob = bob_ctx.new_page()
        alice.goto(f"{base_url}/c/{session_id}")
        bob.goto(f"{base_url}/c/{session_id}")

        bob_circle = alice.get_by_test_id(f"presence-avatar-{bob_email}")
        expect(bob_circle).to_be_visible(timeout=15_000)
        # Active to start: a dimmed circle here means the connect-time
        # idle computation invented an idle state for a visible tab.
        expect(bob_circle).not_to_have_class(re.compile(r"opacity-40"))

        # Background Bob's tab. Playwright can't truly background a
        # headless page, so shadow the document.hidden getter and fire
        # the same event the browser would — exactly what the SPA's
        # module-level visibilitychange listener consumes.
        bob.evaluate(
            "() => {"
            "  Object.defineProperty(document, 'hidden', "
            "    { get: () => true, configurable: true });"
            "  document.dispatchEvent(new Event('visibilitychange'));"
            "}"
        )

        # The grey lands only after the SPA's 30s hidden-debounce plus
        # the reconnect/broadcast round trip — 60s bounds that without
        # masking a stall. (An instant grey would ALSO be a bug — it
        # would mean alt-tabs flicker — but that direction is pinned by
        # the tracker unit tests, not re-asserted here.)
        expect(bob_circle).to_have_class(re.compile(r"opacity-40"), timeout=60_000)

        # Restore visibility: un-greying skips the debounce, so it must
        # land within one reconnect round trip.
        bob.evaluate(
            "() => {"
            "  Object.defineProperty(document, 'hidden', "
            "    { get: () => false, configurable: true });"
            "  document.dispatchEvent(new Event('visibilitychange'));"
            "}"
        )
        expect(bob_circle).not_to_have_class(re.compile(r"opacity-40"), timeout=15_000)
    finally:
        alice_ctx.close()
        bob_ctx.close()
