"""UI journey: Escape in the chat composer must interrupt a working session.

Reproduces the escape-no-interrupt defect: pressing Escape in the chat composer
does not interrupt the agent, while clicking the Interrupt (Stop) button does.

The two affordances gate on different signals in ``web/src/pages/ChatPage.tsx``:

* the Interrupt button (``showInterruptButton = isWorking && !hasDraft``) keys off
  ``isWorking = computeIsWorking(sessionStatus)`` -- the **server** session
  activity (``running`` / ``waiting``);
* the composer's Escape handler keys off the **local** send lifecycle
  (``if (e.key === "Escape" && isStreaming)`` where ``isStreaming = status ===
  "streaming"``).

Those two are not the same. A session can be working (``sessionStatus ==
"running"``) while this tab's local ``status`` never latched to ``"streaming"``:
the client only opens the local streaming lifecycle when a ``running`` status
edge carries a ``response_id`` it can attribute a stream to. A bare
PTY-activity ``running`` edge (which native harnesses emit with no
``response_id``) leaves local ``status`` idle. In that state the Interrupt
button is offered and interrupts, but Escape is a no-op because ``isStreaming``
is false.

Journey (all against the real SPA + live server):

1. open a runner-bound session in chat view,
2. drive it into the reported state by publishing an ``external_session_status``
   edge with ``status: "running"`` and no ``response_id`` -- the same route the
   native-harness status forwarder posts to (see
   ``test_send_while_background_task.py``). The composer shows the idle
   placeholder (local status idle) while the Interrupt button appears (session
   working, ``isWorking`` true),
3. focus the empty composer and press Escape,
4. assert Escape interrupts: an ``interrupt`` event is POSTed to
   ``/v1/sessions/<id>/events`` and the Interrupt button settles -- exactly what
   clicking Interrupt does.

On a build with the bug, step 4 FAILS: Escape posts no interrupt and the session
keeps working, because the handler checks local ``status`` instead of session
activity. The trailing control confirms the Interrupt button interrupts the same
state (the correct, session-activity-gated behavior Escape must match), so a
failure is specifically "Escape is broken", not "interrupt is broken"::

    pytest tests/e2e_ui/chat/test_escape_interrupts_working_session.py
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, Route, expect

# The composer's placeholder when the local send lifecycle is idle. When a local
# send owns the turn (``status == "streaming"``) it instead reads "Send a
# follow-up (queued) - Esc to stop"; the idle text here is the tell that local
# status is idle even though the session is working.
_COMPOSER_PLACEHOLDER_IDLE = "Send a message…"

# Seconds to let a genuine interrupt POST reach the events route after the user
# action. ``stop()`` fires it fire-and-forget, so it lands within a tick; 2s
# absorbs slow-CI scheduling. On the buggy build no interrupt is ever posted.
_INTERRUPT_SETTLE_MS = 2_000


def _publish_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    response_id: str | None = None,
) -> None:
    """Publish a session status through the native-harness events route.

    Mirrors the native status forwarder. Posted with ``httpx`` from the test
    process (not the browser), so it does not hit the page's request route --
    only browser-originated ``interrupt`` POSTs do.

    A ``running`` edge with no ``response_id`` reproduces the bare PTY-activity
    edge: the client marks the session working (``sessionStatus == "running"``,
    so ``isWorking`` is true and the Interrupt button shows) but does NOT open
    the local streaming lifecycle (that needs a ``response_id``), so local
    ``status`` stays idle -- the exact divergence behind this bug.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param status: Session status to publish, e.g. ``"running"``.
    :param response_id: Turn's response id. ``None`` omits the field (the bare
        edge that keeps local status idle).
    :returns: None.
    """
    data: dict[str, object] = {"status": status}
    if response_id is not None:
        data["response_id"] = response_id
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_escape_interrupts_working_session(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Escape in the composer must interrupt a working session, like the button.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session

    # Count browser-originated interrupt events. ``stop()`` (invoked by both the
    # Interrupt button and -- once fixed -- Escape) POSTs ``{"type": "interrupt"}``
    # to ``/v1/sessions/<id>/events``. The status publishes above go via httpx,
    # so they never reach this route; only the SPA's own interrupts do.
    interrupt_posts: list[str] = []

    def _record_interrupts(route: Route) -> None:
        request = route.request
        body = request.post_data or ""
        if request.method == "POST" and '"interrupt"' in body:
            interrupt_posts.append(body)
        route.continue_()

    page.route("**/v1/sessions/*/events", _record_interrupts)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    interrupt_button = page.get_by_role("button", name="Interrupt", exact=True)

    # --- Reach the reported state: the session is WORKING (sessionStatus
    # "running" -> isWorking true) while this tab's local send lifecycle is idle
    # (the bare edge carries no response_id, so no local send latches status to
    # "streaming"). ---
    _publish_status(base_url, session_id, "running")
    # Session is working: the Interrupt (Stop) button is offered -- the UI tells
    # the user they can cancel the working session.
    expect(interrupt_button).to_be_visible(timeout=15_000)
    expect(interrupt_button).to_be_enabled()
    # ...yet the composer is on the IDLE placeholder, not the streaming
    # "Esc to stop" hint: local status is idle. This is the exact mismatch --
    # Escape gates on this local status, the Interrupt button on session activity.
    expect(composer).to_have_attribute("placeholder", _COMPOSER_PLACEHOLDER_IDLE, timeout=15_000)

    # --- The bug: Escape in the composer must interrupt, exactly as the button. ---
    interrupt_posts.clear()
    composer.click()  # focus the empty textarea
    composer.press("Escape")
    # Give a genuine interrupt time to be posted. On the buggy build nothing is.
    page.wait_for_timeout(_INTERRUPT_SETTLE_MS)

    assert interrupt_posts, (
        "Escape in the chat composer did not interrupt the working session: no "
        "interrupt event was POSTed to /v1/sessions/<id>/events after pressing "
        "Escape, even though the session is working (isWorking=true) and the "
        "Interrupt button is offered. ChatPage's Escape handler gates on local "
        'status === "streaming" instead of the session activity the Interrupt '
        "button uses."
    )
    # Escape must also settle the working UI: the Interrupt button reverts to
    # Send once the session is no longer working (stop() flips sessionStatus
    # idle), exactly as clicking Interrupt does.
    expect(interrupt_button).to_be_hidden(timeout=10_000)

    # --- Control: the Interrupt BUTTON interrupts the same working state.
    # Confirms the working state and the interrupt path are real, so the failure
    # above is specifically "Escape is broken", not "interrupt is broken". Runs
    # after Escape on a fixed build (Escape already interrupted, so re-arm). ---
    _publish_status(base_url, session_id, "running")
    expect(interrupt_button).to_be_visible(timeout=15_000)

    interrupt_posts.clear()
    interrupt_button.click()
    page.wait_for_timeout(_INTERRUPT_SETTLE_MS)

    assert interrupt_posts, (
        "control: clicking the Interrupt button posted no interrupt event -- the "
        "working state or interrupt path is not set up as expected"
    )
    expect(interrupt_button).to_be_hidden(timeout=10_000)
